# Copyright 2011 Google Inc. All Rights Reserved.
# Copyright 2022 Aerleon Project All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Parses the generic policy files and return a policy object for acl rendering.
"""

from __future__ import annotations

import datetime
import enum
import os
import pathlib
import sys
from typing import TYPE_CHECKING, Any, Optional

from absl import logging
from ply import lex, yacc

from aerleon.lib import nacaddr, naming

if TYPE_CHECKING:
    from aerleon.lib.policy_builder import PolicyBuilder

DEFINITIONS = None
DEFAULT_DEFINITIONS = './def'
ACTIONS = set(('accept', 'count', 'deny', 'reject', 'next', 'reject-with-tcp-rst'))
PROTOS_WITH_PORTS = frozenset(('tcp', 'udp', 'udplite', 'sctp'))
FLEXIBLE_MATCH_RANGE_ATTRIBUTES = {
    'byte-offset',
    'bit-offset',
    'bit-length',
    'match-start',
    'range',
    'range-except',
    'flexible-range-name',
}
FLEXIBLE_MATCH_START_OPTIONS = {'layer-3', 'layer-4', 'payload'}
_LOGGING = set(('true', 'True', 'syslog', 'local', 'disable', 'log-both'))
_OPTIMIZE = True
_SHADE_CHECK = False
_PRESERVE_ORIGINAL = False
_CURRENT_PARSE_FILENAME = ''
_MAX_TTL = 255
_MIN_TTL = 0


class Error(Exception):
    """Generic error class."""


class FileNotFoundError(Error):
    """Policy file unable to be read."""


class FileReadError(Error):
    """Policy file unable to be read."""


class RecursionTooDeepError(Error):
    """Included files exceed maximum recursion depth."""


class BadIncludePath(Error):
    """Included file unable to load due to path restrictions."""


class ParseError(Error):
    """ParseError in the input."""


class TermAddressExclusionError(Error):
    """Excluded address block is not contained in the accepted address block."""


class TermObjectTypeError(Error):
    """Error with an object passed to Term."""


class TermPortProtocolError(Error):
    """Error when a requested protocol doesn't have any of the requested ports."""


class TermProtocolEtherTypeError(Error):
    """Error when both ether-type & upper-layer protocol matches are requested."""


class TermNoActionError(Error):
    """Error when a term hasn't defined an action."""


class TermInvalidIcmpType(Error):
    """Error when a term has invalid icmp-types specified."""


class InvalidTermActionError(Error):
    """Error when an action is invalid."""


class InvalidTermLoggingError(Error):
    """Error when a option is set for logging."""


class UndefinedAddressError(Error):
    """Error when an undefined address is referenced."""


class NoTermsError(Error):
    """Error when no terms were found."""


class ShadingError(Error):
    """Error when a term is shaded by a prior term."""


class FlexibleMatchError(Error):
    """Error when a term contains an invalid flexible match value."""


class ICMPCodeError(Error):
    """Error when ICMP Codes are used with multiple or invalid types."""


class InvalidTermTTLValue(Error):
    """Error when TTL value is invalid."""


class MixedPortandNonPortProtos(Error):
    """Error when protocols that use ports are mixed with protocols that do not"""


class InvalidNumericProtoValue(Error):
    """Error when protocols are numeric and not between -1 and 255."""


def TranslatePorts(ports, protocols, term_name):
    """Return all ports of all protocols requested.

    Args:
      ports: list of ports, eg ['SMTP', 'DNS', 'HIGH_PORTS']
      protocols: list of protocols, eg ['tcp', 'udp']
      term_name: name of current term, used for warning messages

    Returns:
      ret_array: list of ports tuples such as [(25,25), (53,53), (1024,65535)]

    Note:
      Duplication will be taken care of in Term.CollapsePortList
    """
    ret_array = []
    for proto in protocols:
        for port in ports:
            service_by_proto = DEFINITIONS.GetServiceByProto(port, proto)
            if not service_by_proto:
                logging.warning(
                    'Term %s has service %s which is not defined with '
                    'protocol %s, but will be permitted. Unless intended'
                    ', you should consider splitting the protocols '
                    'into separate terms!',
                    term_name,
                    port,
                    proto,
                )

            for p in [x.split('-') for x in service_by_proto]:
                if len(p) == 1:
                    ret_array.append((int(p[0]), int(p[0])))
                else:
                    ret_array.append((int(p[0]), int(p[1])))
    return ret_array


# classes for storing the object types in the policy files.
class Policy:
    """The policy object contains everything found in a given policy file."""

    def __init__(self, header, terms):
        """Initiator for the Policy object.

        Args:
          header: __main__.Header object. contains comments which should be passed
            on to the rendered acls as well as the type of acls this policy file
            should render to.

          terms: list __main__.Term. an array of Term objects which must be rendered
            in each of the rendered acls.

        Attributes:
          filters: list of tuples containing (header, terms).
        """
        self.filters = []
        self.filename = ''
        self.AddFilter(header, terms)

    def AddFilter(self, header, terms):
        """Add another header & filter."""
        self.filters.append((header, terms))
        if not _PRESERVE_ORIGINAL:
            self._TranslateTerms(terms)
        if _SHADE_CHECK:
            self._DetectShading(terms)

    def _TranslateTerms(self, terms):
        """."""
        if not terms:
            raise NoTermsError('no terms found')
        for term in terms:
            # TODO(pmoody): this probably belongs in Term.SanityCheck(),
            # or at the very least, in some method under class Term()
            if term.translated:
                continue
            if term.port:
                term.port = TranslatePorts(term.port, term.protocol, term.name)
                if not term.port:
                    raise TermPortProtocolError(
                        'no ports of the correct protocol for term %s' % (term.name)
                    )
            if term.source_port:
                term.source_port = TranslatePorts(term.source_port, term.protocol, term.name)
                if not term.source_port:
                    raise TermPortProtocolError(
                        'no source ports of the correct protocol for term %s' % (term.name)
                    )
            if term.destination_port:
                term.destination_port = TranslatePorts(
                    term.destination_port, term.protocol, term.name
                )
                if not term.destination_port:
                    raise TermPortProtocolError(
                        'no destination ports of the correct protocol for term %s' % (term.name)
                    )

            # If argument is true, we optimize, otherwise just sort addresses
            term.AddressCleanup(_OPTIMIZE, self._NeedsAddressBook())
            term.SanityCheck()
            term.translated = True

    def _NeedsAddressBook(self):
        """Returns True if the policy uses a generator needing an addressbook."""
        for header in self.headers:
            if not header:
                continue
            if 'srx' in header.platforms:
                return True
            for target in header.target:
                opts = header.FilterOptions(target.platform)
                if opts and 'object-group' in opts:
                    return True
        return False

    @property
    def headers(self):
        """Returns the headers from each of the configured filters.

        Returns:
          headers
        """
        return [x[0] for x in self.filters]

    def _DetectShading(self, terms):
        """Finds terms which are shaded (impossible to reach).

        Iterate through each term, looking at each prior term. If a prior term
        contains every component of the current term then the current term would
        never be hit and is thus shaded. This can be a mistake.

        Args:
          terms: list of Term objects.
        """
        for index, term in enumerate(terms):
            for prior_index in range(index):
                # Check each term that came before for shading. Terms with next as an
                # action do not terminate evaluation, so cannot shade.
                if term in terms[prior_index] and 'next' not in terms[prior_index].action:
                    logging.warning(f"{term.name} is shaded by {terms[prior_index].name}")

    def __eq__(self, obj):
        """Compares for equality against another Policy object.

        Note that it is picky and requires the list contents to be in the
        same order.

        Args:
          obj: object to be compared to for equality.
        Returns:
          True if the list of filters in this policy object is equal to the list
          in obj and False otherwise.
        """
        if not isinstance(obj, Policy):
            return False
        return self.filters == obj.filters

    def __str__(self):
        def tuple_str(tup):
            return '%s:%s' % (tup[0], tup[1])

        return 'Policy: {%s}' % ', '.join(map(tuple_str, self.filters))

    def __repr__(self):
        return self.__str__()


class Term:
    """The Term object is used to store each of the terms.

    Args:
      obj: an object of type VarType or a list of objects of type VarType

    members:
      address/source_address/destination_address/: list of
        VarType.(S|D)?ADDRESS's
      address_exclude/source_address_exclude/destination_address_exclude: list of
        VarType.(S|D)?ADDEXCLUDE's
      restrict-address-family: VarType.RESTRICT_ADDRESS_FAMILY
      port/source_port/destination_port: list of VarType.(S|D)?PORT's
      options: list of VarType.OPTION's.
      protocol: list of VarType.PROTOCOL's.
      counter: VarType.COUNTER
      traffic-class-count: VarType.TRAFFIC_CLASS_COUNT
      action: list of VarType.ACTION's
      dscp-set: VarType.DSCP_SET
      dscp-match: VarType.DSCP_MATCH
      dscp-except: VarType.DSCP_EXCEPT
      comments: VarType.COMMENT
      encapsulate: VarType.ENCAPSULATE
      filter-term: VarType.FILTER_TERM
      flexible-match-range: VarType.FLEXIBLE_MATCH_RANGE
      forwarding-class: VarType.FORWARDING_CLASS
      forwarding-class-except: VarType.FORWARDING_CLASS_EXCEPT
      expiration: VarType.EXPIRATION
      verbatim: VarType.VERBATIM
      logging: VarType.LOGGING
      log_name: VarType.LOG_NAME
      next-ip: VarType.NEXT_IP
      port-mirror: VarType.PORT_MIRROR
      qos: VarType.QOS
      pan-application: VarType.PAN_APPLICATION
      policer: VarType.POLICER
      priority: VarType.PRIORITY
      destination-zone: VarType.DZONE
      source-zone: VarType.SZONE
      vpn: VarType.VPN
    """

    ICMP_TYPE = {
        4: {
            'echo-reply': 0,
            'unreachable': 3,
            'source-quench': 4,
            'redirect': 5,
            'alternate-address': 6,
            'echo-request': 8,
            'router-advertisement': 9,
            'router-solicitation': 10,
            'time-exceeded': 11,
            'parameter-problem': 12,
            'timestamp-request': 13,
            'timestamp-reply': 14,
            'information-request': 15,
            'information-reply': 16,
            'mask-request': 17,
            'mask-reply': 18,
            'conversion-error': 31,
            'mobile-redirect': 32,
        },
        6: {
            'destination-unreachable': 1,
            'packet-too-big': 2,
            'time-exceeded': 3,
            'parameter-problem': 4,
            'echo-request': 128,
            'echo-reply': 129,
            'multicast-listener-query': 130,
            'multicast-listener-report': 131,
            'multicast-listener-done': 132,
            'router-solicit': 133,
            'router-advertisement': 134,
            'neighbor-solicit': 135,
            'neighbor-advertisement': 136,
            'redirect-message': 137,
            'router-renumbering': 138,
            'icmp-node-information-query': 139,
            'icmp-node-information-response': 140,
            'inverse-neighbor-discovery-solicitation': 141,
            'inverse-neighbor-discovery-advertisement': 142,
            'version-2-multicast-listener-report': 143,
            'home-agent-address-discovery-request': 144,
            'home-agent-address-discovery-reply': 145,
            'mobile-prefix-solicitation': 146,
            'mobile-prefix-advertisement': 147,
            'certification-path-solicitation': 148,
            'certification-path-advertisement': 149,
            'multicast-router-advertisement': 151,
            'multicast-router-solicitation': 152,
            'multicast-router-termination': 153,
        },
    }
    ICMP_CODE = {
        'unreachable': [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
        'redirect': [0, 1, 2, 3],
        'router-advertisement': [0, 16],
        'time-exceeded': [0, 1],
        'destination-unreachable': [0, 1, 2, 3, 4, 5, 6, 7],
        'parameter-problem': [0, 1, 2, 3],
        'router-renumbering': [0, 1, 255],
        'icmp-node-information-query': [0, 1, 2],
        'icmp-node-information-response': [0, 1, 2],
    }
    _IPV4_BYTE_SIZE = 1
    _IPV6_BYTE_SIZE = 4

    def __init__(self, obj):
        self.name = None

        self.action = []
        self.address = []
        self.address_exclude = []
        self.restrict_address_family = None
        self.comment = []
        self.counter = None
        self.expiration = None
        self.destination_address = []
        self.destination_address_exclude = []
        self.destination_port = []
        self.destination_prefix = []
        self.filter_term = None
        self.forwarding_class = []
        self.forwarding_class_except = []
        self.logging = []
        self.log_limit = None
        self.log_name = None
        self.loss_priority = None
        self.option = []
        self.owner = None
        self.policer = None
        self.port = []
        self.precedence = []
        self.protocol = []
        self.protocol_except = []
        self.qos = None
        self.pan_application = []
        self.routing_instance = None
        self.source_address = []
        self.source_address_exclude = []
        self.source_port = []
        self.source_prefix = []
        self.ttl = None
        self.verbatim = []
        # juniper specific.
        self.packet_length = None
        self.fragment_offset = None
        self.hop_limit = None
        self.icmp_type = []
        self.icmp_code = []
        self.ether_type = []
        self.traffic_class_count = None
        self.traffic_type = []
        self.translated = False
        self.dscp_set = None
        self.dscp_match = []
        self.dscp_except = []
        self.next_ip = None
        self.flexible_match_range = []
        self.source_prefix_except = []
        self.destination_prefix_except = []
        self.inactive = False
        self.encapsulate = None
        self.port_mirror = None
        # srx specific
        self.destination_zone = []
        self.source_zone = []
        self.vpn = None
        # gce specific
        self.source_tag = []
        self.destination_tag = []
        self.priority = None
        # iptables specific
        self.source_interface = None
        self.destination_interface = None
        self.platform = []
        self.platform_exclude = []
        self.target_resources = []
        self.target_service_accounts = []
        self.timeout = None
        self.flattened = False
        self.flattened_addr = None
        self.flattened_saddr = None
        self.flattened_daddr = None
        self.stateless_reply = False

        # AddObject touches variables which might not have been initialized
        # further up so this has to be at the end.
        self.AddObject(obj)

    def __contains__(self, other):
        """Determine if other term is contained in this term."""
        if self.verbatim or other.verbatim:
            # short circuit these
            if sorted(list(self.verbatim)) != sorted(other.verbatim):
                return False

        # check protocols
        # either protocol or protocol-except may be used, not both at the same time.
        if self.protocol:
            if other.protocol:
                if not self.CheckProtocolIsContained(other.protocol, self.protocol):
                    return False
            # this term has protocol, other has protocol_except.
            elif other.protocol_except:
                return False
            else:
                # other does not have protocol or protocol_except. since we do other
                # cannot be contained in self.
                return False
        elif self.protocol_except:
            if other.protocol_except:
                if not self.CheckProtocolIsContained(self.protocol_except, other.protocol_except):
                    return False
            elif other.protocol:
                for proto in other.protocol:
                    if proto in self.protocol_except:
                        return False
            else:
                return False

        # combine addresses with exclusions for proper contains comparisons.
        if not self.flattened:
            self.FlattenAll()
        if not other.flattened:
            other.FlattenAll()

        # flat 'address' is compared against other flat (saddr|daddr).
        # if NONE of these evaluate to True other is not contained.
        if not (
            self.CheckAddressIsContained(self.flattened_addr, other.flattened_addr)
            or self.CheckAddressIsContained(self.flattened_addr, other.flattened_saddr)
            or self.CheckAddressIsContained(self.flattened_addr, other.flattened_daddr)
        ):
            return False

        # compare flat address from other to flattened self (saddr|daddr).
        if not (
            # other's flat address needs both self saddr & daddr to contain in order
            # for the term to be contained. We already compared the flattened_addr
            # attributes of both above, which was not contained.
            self.CheckAddressIsContained(other.flattened_addr, self.flattened_saddr)
            and self.CheckAddressIsContained(other.flattened_addr, self.flattened_daddr)
        ):
            return False

        # basic saddr/daddr check.
        if not (self.CheckAddressIsContained(self.flattened_saddr, other.flattened_saddr)):
            return False
        if not (self.CheckAddressIsContained(self.flattened_daddr, other.flattened_daddr)):
            return False

        # check ports
        # like the address directive, the port directive is special in that it can
        # be either source or destination.
        if self.port:
            if not (
                self.CheckPortIsContained(self.port, other.port)
                or self.CheckPortIsContained(self.port, other.source_port)
                or self.CheckPortIsContained(self.port, other.destination_port)
            ):
                return False
        if not self.CheckPortIsContained(self.source_port, other.source_port):
            return False
        if not self.CheckPortIsContained(self.destination_port, other.destination_port):
            return False

        # prefix lists
        if self.source_prefix:
            if sorted(self.source_prefix) != sorted(other.source_prefix):
                return False
        if self.source_prefix_except:
            if sorted(self.source_prefix_except) != sorted(other.source_prefix_except):
                return False
        if self.destination_prefix:
            if sorted(self.destination_prefix) != sorted(other.destination_prefix):
                return False
        if self.destination_prefix_except:
            if sorted(self.destination_prefix_except) != sorted(other.destination_prefix_except):
                return False

        # check source and destination tags
        if self.source_tag:
            if sorted(self.source_tag) != sorted(other.source_tag):
                return False
            if sorted(self.destination_tag) != sorted(other.destination_tag):
                return False

        # check precedence
        if self.precedence:
            if not other.precedence:
                return False
            for precedence in other.precedence:
                if precedence not in self.precedence:
                    return False
        elif other.precedence:
            return False
        # check various options
        if self.option:
            if not other.option:
                return False
            for opt in other.option:
                if opt not in self.option:
                    return False
        elif other.option:
            return False
        # check forwarding-class
        if self.forwarding_class:
            if not other.forwarding_class:
                return False
            for fc in other.forwarding_class:
                if fc not in self.forwarding_class:
                    return False
        # check forwarding-class-except
        if self.forwarding_class_except:
            if not other.forwarding_class_except:
                return False
            for fc in other.forwarding_class_except:
                if fc not in self.forwarding_class_except:
                    return False
        if self.next_ip:
            if not other.next_ip:
                return False
        if self.encapsulate:
            if not other.encapsulate:
                return False
        if self.fragment_offset:
            # fragment_offset looks like 'integer-integer' or just, 'integer'
            sfo = sorted([int(x) for x in self.fragment_offset.split('-')])
            if other.fragment_offset:
                ofo = sorted([int(x) for x in other.fragment_offset.split('-')])
                if ofo[0] < sfo[0] or sfo[1:] < ofo[1:]:
                    return False
            else:
                return False
        if self.hop_limit:
            # hop_limit looks like 'integer-integer' or just, 'integer'
            shl = [int(x) for x in self.hop_limit.split('-')]
            if other.hop_limit:
                ohl = [int(x) for x in other.hop_limit.split('-')]
                if shl[0] < ohl[0]:
                    return False
                shll, ohll = shl[1:2], ohl[1:2]
                if shll and ohll:
                    if shl[0] > ohl[0]:
                        return False
            else:
                return False
        if self.packet_length:
            # packet_length looks like 'integer-integer' or just, 'integer'
            spl = [int(x) for x in self.packet_length.split('-')]
            if other.packet_length:
                opl = [int(x) for x in other.packet_length.split('-')]
                if spl[0] < opl[0] or sorted(spl[1:]) > sorted(opl[1:]):
                    return False
            else:
                return False
        if self.port_mirror:
            if not other.port_mirror:
                return False
        if self.icmp_type:
            if sorted(self.icmp_type) is not sorted(other.icmp_type):
                return False

        if self.icmp_code:
            if sorted(self.icmp_code) is not sorted(other.icmp_code):
                return False

        # check platform
        if self.platform:
            if sorted(self.platform) is not sorted(other.platform):
                return False
        if self.platform_exclude:
            if sorted(self.platform_exclude) is not sorted(other.platform_exclude):
                return False

        if self.source_zone:
            if sorted(self.source_zone) is not sorted(other.source_zone):
                return False

        if self.destination_zone:
            if sorted(self.destination_zone) is not sorted(other.destination_zone):
                return False

        # we have containment
        return True

    def __str__(self):
        ret_str = []
        ret_str.append(' name: %s' % self.name)
        if self.address:
            ret_str.append('  address: %s' % sorted(self.address))
        if self.address_exclude:
            ret_str.append('  address_exclude: %s' % sorted(self.address_exclude))
        if self.source_address:
            ret_str.append('  source_address: %s' % self._SortAddressesByFamily('source_address'))
        if self.source_address_exclude:
            ret_str.append(
                '  source_address_exclude: %s'
                % self._SortAddressesByFamily('source_address_exclude')
            )
        if self.source_tag:
            ret_str.append('  source_tag: %s' % self.source_tag)
        if self.destination_address:
            ret_str.append(
                '  destination_address: %s' % self._SortAddressesByFamily('destination_address')
            )
        if self.destination_address_exclude:
            ret_str.append(
                '  destination_address_exclude: %s'
                % self._SortAddressesByFamily('destination_address_exclude')
            )
        if self.destination_tag:
            ret_str.append('  destination_tag: %s' % self.destination_tag)
        if self.target_resources:
            ret_str.append('  target_resources: %s' % self.target_resources)
        if self.target_service_accounts:
            ret_str.append('  target_service_accounts: %s' % self.target_service_accounts)
        if self.source_prefix:
            ret_str.append('  source_prefix: %s' % self.source_prefix)
        if self.source_prefix_except:
            ret_str.append('  source_prefix_except: %s' % self.source_prefix_except)
        if self.destination_prefix:
            ret_str.append('  destination_prefix: %s' % self.destination_prefix)
        if self.destination_prefix_except:
            ret_str.append('  destination_prefix_except: %s' % self.destination_prefix_except)
        if self.filter_term:
            ret_str.append('  filter_term: %s' % self.filter_term)
        if self.forwarding_class:
            ret_str.append('  forwarding_class: %s' % self.forwarding_class)
        if self.forwarding_class_except:
            ret_str.append('  forwarding_class_except: %s' % self.forwarding_class_except)
        if self.icmp_type:
            ret_str.append('  icmp_type: %s' % sorted(self.icmp_type))
        if self.icmp_code:
            ret_str.append('  icmp_code: %s' % sorted(self.icmp_code))
        if self.next_ip:
            ret_str.append('  next_ip: %s' % self.next_ip)
        if self.encapsulate:
            ret_str.append('  encapsulate: %s' % self.encapsulate)
        if self.protocol:
            ret_str.append('  protocol: %s' % sorted(self.protocol))
        if self.protocol_except:
            ret_str.append('  protocol-except: %s' % self.protocol_except)
        if self.owner:
            ret_str.append('  owner: %s' % self.owner)
        if self.port:
            ret_str.append('  port: %s' % sorted(self.port))
        if self.port_mirror:
            ret_str.append('  port_mirror: %s' % self.port_mirror)
        if self.source_port:
            ret_str.append('  source_port: %s' % sorted(self.source_port))
        if self.destination_port:
            ret_str.append('  destination_port: %s' % sorted(self.destination_port))
        if self.action:
            ret_str.append('  action: %s' % self.action)
        if self.option:
            ret_str.append('  option: %s' % self.option)
        if self.flexible_match_range:
            ret_str.append('  flexible_match_range: %s' % self.flexible_match_range)
        if self.qos:
            ret_str.append('  qos: %s' % self.qos)
        if self.pan_application:
            ret_str.append('  pan_application: %s' % self.pan_application)
        if self.logging:
            ret_str.append('  logging: %s' % self.logging)
        if self.log_limit:
            ret_str.append('  log_limit: %s/%s' % (self.log_limit[0], self.log_limit[1]))
        if self.log_name:
            ret_str.append('  log_name: %s' % self.log_name)
        if self.priority:
            ret_str.append('  priority: %s' % self.priority)
        if self.counter:
            ret_str.append('  counter: %s' % self.counter)
        if self.traffic_class_count:
            ret_str.append('  traffic_class_count: %s' % self.traffic_class_count)
        if self.source_interface:
            ret_str.append('  source_interface: %s' % self.source_interface)
        if self.destination_interface:
            ret_str.append('  destination_interface: %s' % self.destination_interface)
        if self.expiration:
            ret_str.append('  expiration: %s' % self.expiration)
        if self.platform:
            ret_str.append('  platform: %s' % self.platform)
        if self.platform_exclude:
            ret_str.append('  platform_exclude: %s' % self.platform_exclude)
        if self.ttl:
            ret_str.append('  ttl: %s' % self.ttl)
        if self.timeout:
            ret_str.append('  timeout: %s' % self.timeout)
        if self.vpn:
            vpn_name, pair_policy = self.vpn
            if pair_policy:
                ret_str.append('  vpn: name = %s, pair_policy = %s' % (vpn_name, pair_policy))
            else:
                ret_str.append('  vpn: name = %s' % vpn_name)
        if self.source_zone:
            ret_str.append('  source_zone: %s' % sorted(self.source_zone))
        if self.destination_zone:
            ret_str.append('  destination_zone: %s' % sorted(self.destination_zone))

        return '\n'.join(ret_str)

    def __repr__(self):
        return self.__str__()

    def __eq__(self, other):
        # action
        if sorted(self.action) != sorted(other.action):
            return False

        # addresses.
        if not (
            sorted(self.address) == sorted(other.address)
            and sorted(self.source_address) == sorted(other.source_address)
            and sorted(self.source_address_exclude) == sorted(other.source_address_exclude)
            and sorted(self.destination_address) == sorted(other.destination_address)
            and sorted(self.destination_address_exclude)
            == sorted(other.destination_address_exclude)
        ):
            return False

        # prefix lists
        if not (
            sorted(self.source_prefix) == sorted(other.source_prefix)
            and sorted(self.source_prefix_except) == sorted(other.source_prefix_except)
            and sorted(self.destination_prefix) == sorted(other.destination_prefix)
            and sorted(self.destination_prefix_except) == sorted(other.destination_prefix_except)
        ):
            return False

        # ports
        if not (
            sorted(self.port) == sorted(other.port)
            and sorted(self.source_port) == sorted(other.source_port)
            and sorted(self.destination_port) == sorted(other.destination_port)
        ):
            return False

        # protocol
        if not (
            sorted(self.protocol) == sorted(other.protocol)
            and sorted(self.protocol_except) == sorted(other.protocol_except)
        ):
            return False

        # option
        if sorted(self.option) != sorted(other.option):
            return False

        # qos
        if self.qos != other.qos:
            return False

        # pan-application
        if sorted(self.pan_application) != sorted(other.pan_application):
            return False

        # verbatim
        if self.verbatim != other.verbatim:
            return False

        # policer
        if self.policer != other.policer:
            return False

        # interface
        if self.source_interface != other.source_interface:
            return False

        if self.destination_interface != other.destination_interface:
            return False

        # tags
        if not (
            sorted(self.source_tag) == sorted(other.source_tag)
            and sorted(self.destination_tag) == sorted(other.destination_tag)
        ):
            return False

        if self.ttl != other.ttl:
            return False

        if sorted(self.logging) != sorted(other.logging):
            return False
        if self.log_limit != other.log_limit:
            return False
        if self.qos != other.qos:
            return False
        if sorted(self.pan_application) != sorted(other.pan_application):
            return False
        if self.packet_length != other.packet_length:
            return False
        if self.fragment_offset != other.fragment_offset:
            return False
        if self.hop_limit != other.hop_limit:
            return False
        if sorted(self.icmp_type) != sorted(other.icmp_type):
            return False
        if sorted(self.icmp_code) != sorted(other.icmp_code):
            return False
        if sorted(self.ether_type) != sorted(other.ether_type):
            return False
        if sorted(self.traffic_type) != sorted(other.traffic_type):
            return False

        # vpn
        if self.vpn != other.vpn:
            return False

        # platform
        if not (
            sorted(self.platform) == sorted(other.platform)
            and sorted(self.platform_exclude) == sorted(other.platform_exclude)
        ):
            return False

        # timeout
        if self.timeout != other.timeout:
            return False

        # precedence
        if self.precedence != other.precedence:
            return False

        # filter
        if self.filter_term != other.filter_term:
            return False

        # forwarding-class
        if sorted(self.forwarding_class) != sorted(other.forwarding_class):
            return False

        # forwarding-class-except
        if sorted(self.forwarding_class_except) != sorted(other.forwarding_class_except):
            return False

        # next_ip
        if self.next_ip != other.next_ip:
            return False

        # encapsulate
        if self.encapsulate != other.encapsulate:
            return False

        # flexible_match
        if self.flexible_match_range != other.flexible_match_range:
            return False

        # port_mirror
        if self.port_mirror != other.port_mirror:
            return False

        # source_zone
        if sorted(self.source_zone) != sorted(other.source_zone):
            return False

        # destination_zone
        if sorted(self.destination_zone) != sorted(other.destination_zone):
            return False

        return True

    def __ne__(self, other):
        return not self.__eq__(other)

    def _SortAddressesByFamily(self, addr_type):
        """Provide the term address field to sort.

        Method will sort v4 and then concatenate sorted v6 addresses. This will
        support Term.__str__ function which outputs a string of
        sorted IP addresses.

        Args:
          addr_type: string, this will be either 'source_address',
            'source_address_exclude', 'destination_address' or
            'destination_address_exclude'
        Returns:
          List of IP addresses sourted v4 then v6
        """
        # Sort v4 and v6
        sort_v4 = sorted(self.GetAddressOfVersion(addr_type, 4))
        sort_v6 = sorted(self.GetAddressOfVersion(addr_type, 6))

        # Concatenate
        return sort_v4 + sort_v6

    def AddressesByteLength(self, address_family=(4, 6)):
        """Returns the byte length of all IP addresses in the term.

        This is used in the srx generator due to a address size limitation.

        Args:
          address_family: Address families to include for determining byte length.

        Returns:
          counter: Byte length of the sum of both source and destination IPs.
        """
        counter = 0
        for i in self.source_address:
            if i.version == 6 and i.version in address_family:
                counter += self._IPV6_BYTE_SIZE
            elif i.version == 4 and i.version in address_family:
                counter += self._IPV4_BYTE_SIZE
        for i in self.destination_address:
            if i.version == 6 and i.version in address_family:
                counter += self._IPV6_BYTE_SIZE
            elif i.version == 4 and i.version in address_family:
                counter += self._IPV4_BYTE_SIZE
        return counter

    def FlattenAll(self, mutate=True):
        """Reduce source, dest, and address fields to their post-exclude state.

        Populates the self.flattened_addr, self.flattened_saddr,
        self.flattened_daddr by removing excludes from includes.

        Args:
          mutate: Boolean value indicating if this method should mutate the original
                  address (address, destination_address, source_address)
        """
        # No excludes, set flattened attributes and move along.
        self.flattened = True
        if not (
            self.source_address_exclude or self.destination_address_exclude or self.address_exclude
        ):
            self.flattened_saddr = self.source_address
            self.flattened_daddr = self.destination_address
            self.flattened_addr = self.address
            return

        if self.source_address_exclude:
            self.flattened_saddr = nacaddr.AddressListExclude(
                self.source_address, self.source_address_exclude, collapse_addrs=False
            )
            if mutate:
                self.source_address = self.flattened_saddr
        if self.destination_address_exclude:
            self.flattened_daddr = nacaddr.AddressListExclude(
                self.destination_address, self.destination_address_exclude, collapse_addrs=False
            )
            if mutate:
                self.destination_address = self.flattened_daddr
        if self.address_exclude:
            self.flattened_addr = nacaddr.AddressListExclude(
                self.address, self.address_exclude, collapse_addrs=False
            )
            if mutate:
                self.address = self.flattened_addr

    def GetAddressOfVersion(self, addr_type, af=None):
        """Returns addresses of the appropriate Address Family.

        Args:
          addr_type: string, this will be either
            'source_address', 'source_address_exclude',
            'destination_address' or 'destination_address_exclude'
          af: int or None, either 4 for IPv4 or 6 for IPv6

        Returns:
          list of addresses of the correct family.
        """
        if not af:
            return getattr(self, addr_type)

        return [x for x in getattr(self, addr_type) if x.version == af]

    def AddObject(self, obj):
        """Add an object of unknown type to this term.

        Args:
          obj: single or list of either
            [Address, Port, Option, Protocol, Counter, Action, Comment, Expiration]

        Raises:
          InvalidTermActionError: if the action defined isn't an accepted action.
            eg, action:: godofoobar
          TermObjectTypeError: if AddObject is called with an object it doesn't
            understand.
          InvalidTermLoggingError: when a option is set for logging not known.
        """
        if type(obj) is list:
            for x in obj:
                # do we have a list of addresses?
                # expanded address fields consolidate naked address fields with
                # saddr/daddr.
                if x.var_type is VarType.SADDRESS:
                    saddr = DEFINITIONS.GetNetAddr(x.value)
                    self.source_address.extend(saddr)
                elif x.var_type is VarType.DADDRESS:
                    daddr = DEFINITIONS.GetNetAddr(x.value)
                    self.destination_address.extend(daddr)
                elif x.var_type is VarType.ADDRESS:
                    addr = DEFINITIONS.GetNetAddr(x.value)
                    self.address.extend(addr)
                # do we have address excludes?
                elif x.var_type is VarType.SADDREXCLUDE:
                    saddr_exclude = DEFINITIONS.GetNetAddr(x.value)
                    self.source_address_exclude.extend(saddr_exclude)
                elif x.var_type is VarType.DADDREXCLUDE:
                    daddr_exclude = DEFINITIONS.GetNetAddr(x.value)
                    self.destination_address_exclude.extend(daddr_exclude)
                elif x.var_type is VarType.ADDREXCLUDE:
                    addr_exclude = DEFINITIONS.GetNetAddr(x.value)
                    self.address_exclude.extend(addr_exclude)
                # do we have a list of ports?
                elif x.var_type is VarType.PORT:
                    self.port.append(x.value)
                elif x.var_type is VarType.SPORT:
                    self.source_port.append(x.value)
                elif x.var_type is VarType.DPORT:
                    self.destination_port.append(x.value)
                # do we have a list of protocols?
                elif x.var_type is VarType.PROTOCOL:
                    self.protocol.append(x.value)
                # do we have a list of protocol-exceptions?
                elif x.var_type is VarType.PROTOCOL_EXCEPT:
                    self.protocol_except.append(x.value)
                # do we have a list of options?
                elif x.var_type is VarType.OPTION:
                    self.option.append(x.value)
                elif x.var_type is VarType.SPFX:
                    self.source_prefix.append(x.value)
                elif x.var_type is VarType.ESPFX:
                    self.source_prefix_except.append(x.value)
                elif x.var_type is VarType.DPFX:
                    self.destination_prefix.append(x.value)
                elif x.var_type is VarType.EDPFX:
                    self.destination_prefix_except.append(x.value)
                elif x.var_type is VarType.ETHER_TYPE:
                    self.ether_type.append(x.value)
                elif x.var_type is VarType.TRAFFIC_TYPE:
                    self.traffic_type.append(x.value)
                elif x.var_type is VarType.PRECEDENCE:
                    self.precedence.append(x.value)
                elif x.var_type is VarType.FORWARDING_CLASS:
                    self.forwarding_class.append(x.value)
                elif x.var_type is VarType.FORWARDING_CLASS_EXCEPT:
                    self.forwarding_class_except.append(x.value)
                elif x.var_type is VarType.PAN_APPLICATION:
                    self.pan_application.append(x.value)
                elif x.var_type is VarType.NEXT_IP:
                    self.next_ip = DEFINITIONS.GetNetAddr(x.value)
                elif x.var_type is VarType.PLATFORM:
                    self.platform.append(x.value)
                elif x.var_type is VarType.PLATFORMEXCLUDE:
                    self.platform_exclude.append(x.value)
                elif x.var_type is VarType.DSCP_MATCH:
                    self.dscp_match.append(x.value)
                elif x.var_type is VarType.DSCP_EXCEPT:
                    self.dscp_except.append(x.value)
                elif x.var_type is VarType.STAG:
                    self.source_tag.append(x.value)
                elif x.var_type is VarType.DTAG:
                    self.destination_tag.append(x.value)
                elif x.var_type is VarType.FLEXIBLE_MATCH_RANGE:
                    self.flexible_match_range.append(x.value)
                elif x.var_type is VarType.TARGET_RESOURCES:
                    self.target_resources.append(x.value)
                elif x.var_type is VarType.TARGET_SERVICE_ACCOUNTS:
                    self.target_service_accounts.append(x.value)
                elif x.var_type is VarType.SZONE:
                    self.source_zone.append(x.value)
                elif x.var_type is VarType.DZONE:
                    self.destination_zone.append(x.value)
                else:
                    raise TermObjectTypeError(
                        '%s isn\'t a type I know how to deal with (contains \'%s\')'
                        % (type(x), x.value)
                    )
        else:
            # stupid no switch statement in python
            if obj.var_type is VarType.RESTRICT_ADDRESS_FAMILY:
                self.restrict_address_family = obj.value
            elif obj.var_type is VarType.COMMENT:
                self.comment.append(str(obj))
            elif obj.var_type is VarType.OWNER:
                self.owner = obj.value
            elif obj.var_type is VarType.EXPIRATION:
                self.expiration = obj.value
            elif obj.var_type is VarType.LOSS_PRIORITY:
                self.loss_priority = obj.value
            elif obj.var_type is VarType.ROUTING_INSTANCE:
                self.routing_instance = obj.value
            elif obj.var_type is VarType.PRECEDENCE:
                self.precedence = obj.value
            elif obj.var_type is VarType.FORWARDING_CLASS:
                self.forwarding_class.append(obj.value)
            elif obj.var_type is VarType.FORWARDING_CLASS_EXCEPT:
                self.forwarding_class_except.append(obj.value)
            elif obj.var_type is VarType.PAN_APPLICATION:
                self.pan_application.append(obj.value)
            elif obj.var_type is VarType.NEXT_IP:
                self.next_ip = DEFINITIONS.GetNetAddr(obj.value)
            elif obj.var_type is VarType.VERBATIM:
                self.verbatim.append(obj.value)
            elif obj.var_type is VarType.ACTION:
                if str(obj) not in ACTIONS:
                    raise InvalidTermActionError('%s is not a valid action' % obj)
                self.action.append(obj.value)
            elif obj.var_type is VarType.COUNTER:
                self.counter = obj
            elif obj.var_type is VarType.ENCAPSULATE:
                self.encapsulate = obj.value
            elif obj.var_type is VarType.PORT_MIRROR:
                self.port_mirror = obj.value
            elif obj.var_type is VarType.TRAFFIC_CLASS_COUNT:
                self.traffic_class_count = obj
            elif obj.var_type is VarType.ICMP_TYPE:
                self.icmp_type.extend(obj.value)
            elif obj.var_type is VarType.ICMP_CODE:
                self.icmp_code.extend(obj.value)
            elif obj.var_type is VarType.LOGGING:
                if str(obj) not in _LOGGING:
                    raise InvalidTermLoggingError('%s is not a valid logging option' % obj)
                self.logging.append(obj)
            elif obj.var_type is VarType.LOG_LIMIT:
                self.log_limit = obj.value
            elif obj.var_type is VarType.LOG_NAME:
                self.log_name = obj.value
            # police man, tryin'a take you jail
            elif obj.var_type is VarType.POLICER:
                self.policer = obj.value
            elif obj.var_type is VarType.PRIORITY:
                self.priority = obj.value
            # qos?
            elif obj.var_type is VarType.QOS:
                self.qos = obj.value
            elif obj.var_type is VarType.PACKET_LEN:
                self.packet_length = obj.value
            elif obj.var_type is VarType.FRAGMENT_OFFSET:
                self.fragment_offset = obj.value
            elif obj.var_type is VarType.HOP_LIMIT:
                self.hop_limit = obj.value
            elif obj.var_type is VarType.SINTERFACE:
                self.source_interface = obj.value
            elif obj.var_type is VarType.DINTERFACE:
                self.destination_interface = obj.value
            elif obj.var_type is VarType.TIMEOUT:
                self.timeout = obj.value
            elif obj.var_type is VarType.DSCP_SET:
                self.dscp_set = obj.value
            elif obj.var_type is VarType.VPN:
                self.vpn = (obj.value[0], obj.value[1])
            elif obj.var_type is VarType.TTL:
                self.ttl = int(obj.value)
            elif obj.var_type is VarType.TARGET_RESOURCES:
                self.target_resources.append(obj.value)
            elif obj.var_type is VarType.TARGET_SERVICE_ACCOUNTS:
                self.target_service_accounts.append(obj.value)
            elif obj.var_type is VarType.FILTER_TERM:
                self.filter_term = obj.value
            else:
                raise TermObjectTypeError('%s isn\'t a type I know how to deal with' % (type(obj)))

    def SanityCheck(self):
        """Sanity check the definition of the term.

        Raises:
          ParseError: if term has both verbatim and non-verbatim tokens
          TermInvalidIcmpType: if term has invalid icmp-types specified
          TermNoActionError: if the term doesn't have an action defined.
          TermPortProtocolError: if the term has a service/protocol definition pair
            which don't match up, eg. SNMP and tcp
          TermAddressExclusionError: if one of the *-exclude directives is defined,
            but that address isn't contained in the non *-exclude directive. eg:
            source-address::CORP_INTERNAL source-exclude:: LOCALHOST
          TermProtocolEtherTypeError: if the term has both ether-type and
            upper-layer protocol restrictions
          InvalidTermActionError: action and routing-instance both defined
          InvalidTermTTLValue: TTL value is invalid.
          MixedPortandNonPortProtos: Ports specified with protocol that doesn't
            support ports.

        This should be called when the term is fully formed, and
        all of the options are set.

        """
        if self.verbatim:
            if (
                self.action
                or self.source_port
                or self.destination_port
                or self.port
                or self.protocol
                or self.option
            ):
                raise ParseError(
                    'term "%s" has both verbatim and non-verbatim tokens.' % self.name
                )
        else:
            if (
                not self.action
                and not self.routing_instance
                and not self.next_ip
                and not self.encapsulate
                and not self.filter_term
                and not self.port_mirror
            ):
                raise TermNoActionError('no action specified for term %s' % self.name)
            if self.filter_term and self.action:
                raise InvalidTermActionError(
                    'term "%s" has both filter and action tokens.' % self.name
                )
            # have we specified a port with a protocol that doesn't support ports?
            protos_no_ports = {p for p in self.protocol if p not in PROTOS_WITH_PORTS}
            if protos_no_ports != set() and (
                self.source_port or self.destination_port or self.port
            ):
                if set(self.protocol) - protos_no_ports != set():
                    # This is a more specific error - some protocols support, but not all
                    raise MixedPortandNonPortProtos(
                        'Term %s contains mixed uses of protocols with and without port '
                        'numbers\nProtocols: %s' % (self.name, self.protocol)
                    )
                else:
                    raise TermPortProtocolError(
                        'ports specified with protocol(s) that don\'t support ports. '
                        'Term: %s Protocols: %s ' % (self.name, protos_no_ports)
                    )

        # TODO(pmoody): do we have mutually exclusive options?
        # eg. tcp-established + tcp-initial?

        if self.ether_type and (
            self.protocol
            or self.address
            or self.destination_address
            or self.destination_address_exclude
            or self.destination_port
            or self.destination_prefix
            or self.destination_prefix_except
            or self.source_address
            or self.source_address_exclude
            or self.source_port
            or self.source_prefix
            or self.source_prefix_except
        ):
            raise TermProtocolEtherTypeError(
                'ether-type not supported when used with upper-layer protocol '
                'restrictions. Term: %s' % self.name
            )
        # validate icmp-types if specified, but addr_family will have to be checked
        # in the generators as policy module doesn't know about that at this point.
        if self.icmp_code:
            if len(self.icmp_type) != 1:
                raise ICMPCodeError(
                    'ICMP Code used with invalid number of types.'
                    'Use only one ICMP type.\n Term: %s' % self.name
                )
            type_name = self.icmp_type[0]
            bad_codes = []
            for code in self.icmp_code:
                if code not in self.ICMP_CODE[type_name]:
                    bad_codes.append(code)
            if bad_codes:
                raise ICMPCodeError(
                    'ICMP Codes %s are invalid for ICMP Type %s.'
                    '\nTerm: %s' % (bad_codes, type_name, self.name)
                )
        if self.icmp_type:
            for icmptype in self.icmp_type:
                if icmptype not in self.ICMP_TYPE[4] and icmptype not in self.ICMP_TYPE[6]:
                    raise TermInvalidIcmpType(
                        'Term %s contains an invalid icmp-type:' '%s' % (self.name, icmptype)
                    )

        if self.ttl:
            if not _MIN_TTL <= self.ttl <= _MAX_TTL:

                raise InvalidTermTTLValue(
                    'Term %s contains invalid TTL: %s' % (self.name, self.ttl)
                )
        for proto in self.protocol:
            if proto.isnumeric():
                if int(proto) < 0 or 255 < int(proto):
                    raise InvalidNumericProtoValue(
                        f'Term {self.name} has protocol={self.protocol}. Numeric protocol values must be between 0 and 255.'
                    )

    def AddressCleanup(self, optimize=True, addressbook=False):
        """Do Address and Port collapsing.

        Notes:
          Collapses both the address definitions and the port definitions
          to their smallest possible length.

        Args:
          optimize: boolean value indicating whether to optimize addresses
          addressbook: Boolean indicating if addressbook is used.
        """

        def cleanup(addresses, complement_addresses):
            if not optimize:
                return nacaddr.SortAddrList(addresses)
            if addressbook:
                return nacaddr.CollapseAddrListPreserveTokens(addresses)
            else:
                return nacaddr.CollapseAddrList(addresses, complement_addresses)

        # address collapsing.
        if self.address:
            self.address = cleanup(self.address, None)

        if self.source_address:
            self.source_address = cleanup(self.source_address, self.source_address_exclude)
        if self.source_address_exclude:
            self.source_address_exclude = cleanup(self.source_address_exclude, self.source_address)

        if self.destination_address:
            self.destination_address = cleanup(
                self.destination_address, self.destination_address_exclude
            )
        if self.destination_address_exclude:
            self.destination_address_exclude = cleanup(
                self.destination_address_exclude, self.destination_address
            )

        # port collapsing.
        if self.port:
            self.port = self.CollapsePortList(self.port)
        if self.source_port:
            self.source_port = self.CollapsePortList(self.source_port)
        if self.destination_port:
            self.destination_port = self.CollapsePortList(self.destination_port)

    def CollapsePortList(self, ports):
        """Given a list of ports, Collapse to the smallest required.

        Args:
          ports: a list of port strings eg: [(80,80), (53,53) (2000, 2009),
                                             (1024,65535)]

        Returns:
          ret_array: the collapsed sorted list of ports, eg: [(53,53), (80,80),
                                                              (1024,65535)]
        """
        ret_ports = []
        for port in sorted(ports):
            if not ret_ports:
                ret_ports.append(port)
            elif ret_ports[-1][1] >= port[1]:
                # (10, 20) and (12, 13) -> (10, 20)
                pass
            elif port[0] < ret_ports[-1][1] < port[1]:
                # (10, 20) and (15, 30) -> (10, 30)
                ret_ports[-1] = (ret_ports[-1][0], port[1])
            elif ret_ports[-1][1] + 1 == port[0]:
                # (10, 20) and (21, 30) -> (10, 30)
                ret_ports[-1] = (ret_ports[-1][0], port[1])
            else:
                # (10, 20) and (22, 30) -> (10, 20), (22, 30)
                ret_ports.append(port)
        return ret_ports

    def CheckProtocolIsContained(self, superset, subset):
        """Check if the given list of protocols is wholly contained.

        Args:
          superset: list of protocols
          subset: list of protocols

        Returns:
          bool: True if subset is contained in superset. false otherwise.
        """
        if not superset:
            return True
        if not subset:
            return False

        # Convert these lists to sets to use set comparison.
        sup = set(superset)
        sub = set(subset)
        return sub.issubset(sup)

    def CheckPortIsContained(self, superset, subset):
        """Check if the given list of ports is wholly contained.

        Args:
          superset: list of port tuples
          subset: list of port tuples

        Returns:
          bool: True if subset is contained in superset, false otherwise
        """
        if not superset:
            return True
        if not subset:
            return False

        for sub_port in subset:
            not_contains = True
            for sup_port in superset:
                if int(sub_port[0]) >= int(sup_port[0]) and int(sub_port[1]) <= int(sup_port[1]):
                    not_contains = False
                    break
            if not_contains:
                return False
        return True

    def CheckAddressIsContained(self, superset, subset):
        """Check if subset is wholey contained by superset.

        Args:
          superset: list of the superset addresses
          subset: list of the subset addresses

        Returns:
          True or False.
        """
        if not superset:
            return True
        if not subset:
            return False

        for sub_addr in subset:
            sub_contained = False
            for sup_addr in superset:
                # ipaddr ensures that version numbers match for inclusion.
                if sub_addr.subnet_of(sup_addr):
                    sub_contained = True
                    break
            if not sub_contained:
                return False
        return True


class VarType:
    """Generic object meant to store lots of basic policy types."""

    COMMENT = 0
    COUNTER = 1
    ACTION = 2
    SADDRESS = 3
    DADDRESS = 4
    ADDRESS = 5
    SPORT = 6
    DPORT = 7
    PROTOCOL_EXCEPT = 8
    OPTION = 9
    PROTOCOL = 10
    SADDREXCLUDE = 11
    DADDREXCLUDE = 12
    LOGGING = 13
    QOS = 14
    POLICER = 15
    PACKET_LEN = 16
    FRAGMENT_OFFSET = 17
    ICMP_TYPE = 18
    SPFX = 19
    DPFX = 20
    ETHER_TYPE = 21
    TRAFFIC_TYPE = 22
    VERBATIM = 23
    LOSS_PRIORITY = 24
    ROUTING_INSTANCE = 25
    PRECEDENCE = 26
    SINTERFACE = 27
    EXPIRATION = 28
    DINTERFACE = 29
    PLATFORM = 30
    PLATFORMEXCLUDE = 31
    PORT = 32
    TIMEOUT = 33
    OWNER = 34
    ADDREXCLUDE = 36
    VPN = 37
    APPLY_GROUPS = 38
    APPLY_GROUPS_EXCEPT = 39
    DSCP_SET = 40
    DSCP_MATCH = 41
    DSCP_EXCEPT = 42
    FORWARDING_CLASS = 43
    STAG = 44
    DTAG = 45
    NEXT_IP = 46
    HOP_LIMIT = 47
    LOG_NAME = 48
    FLEXIBLE_MATCH_RANGE = 49
    ESPFX = 50
    EDPFX = 51
    FORWARDING_CLASS_EXCEPT = 52
    TRAFFIC_CLASS_COUNT = 53
    PAN_APPLICATION = 54
    ICMP_CODE = 55
    PRIORITY = 56
    TTL = 57
    LOG_LIMIT = 58
    TARGET_RESOURCES = 59
    TARGET_SERVICE_ACCOUNTS = 60
    ENCAPSULATE = 61
    FILTER_TERM = 62
    RESTRICT_ADDRESS_FAMILY = 63
    PORT_MIRROR = 64
    SZONE = 65
    DZONE = 66
    TARGET = 67

    def __init__(self, var_type, value, field_comment=None):
        self.var_type = var_type
        if self.var_type == self.COMMENT or self.var_type == self.LOG_NAME:
            # remove the double quotes
            val = str(value).strip('"')
            # make all of the lines start w/o leading whitespace.
            self.value = '\n'.join([x.lstrip() for x in val.splitlines()])
        else:
            self.value = value
        self.field_comment = field_comment

    def __str__(self):
        return str(self.value)

    def __repr__(self):
        return self.__str__()

    def __eq__(self, other):
        return self.var_type == other.var_type and self.value == other.value

    def __hash__(self):
        return id(self)


class Header:
    """The header of the policy file contains the targets and a global comment."""

    def __init__(self):
        self.target = []
        self.comment = []
        self.apply_groups = []
        self.apply_groups_except = []

    def AddObject(self, obj):
        """Add and object to the Header.

        Args:
          obj: of type VarType.COMMENT, VarType.APPLY_GROUPS,
          VarType.APPLY_GROUPS_EXCEPT, or Target

        Raises:
          RuntimeError: if object type cannot be determined
        """
        if type(obj) == Target:
            self.target.append(obj)
        elif isinstance(obj, list) and all(isinstance(x, VarType) for x in obj):
            for x in obj:
                if x.var_type == VarType.APPLY_GROUPS:
                    self.apply_groups.append(str(x))
                elif x.var_type == VarType.APPLY_GROUPS_EXCEPT:
                    self.apply_groups_except.append(str(x))
        elif obj.var_type == VarType.COMMENT:
            self.comment.append(str(obj))
        else:
            raise RuntimeError('Unable to add object from header.')

    @property
    def platforms(self):
        """The platform targets of this particular header."""
        return [x.platform for x in self.target]

    def FilterOptions(self, platform):
        """Given a platform return the options.

        Args:
          platform: string

        Returns:
          list or None
        """
        for target in self.target:
            if target.platform == platform:
                return target.options
        return []

    def FilterName(self, platform):
        """Given a filter_type, return the filter name.

        Args:
          platform: string

        Returns:
          filter_name: string or None

        Notes:
          !! Deprecated in favor of Header.FilterOptions(platform) !!
        """
        for target in self.target:
            if target.platform == platform:
                if target.options:
                    if platform in ['srx', 'paloalto']:
                        if len(target.options) >= 3:
                            return '%s>%s' % (target.options[1], target.options[3])
                        else:
                            return None
                    else:
                        return target.options[0]
        return None

    def __str__(self):
        return 'Target[%s], Comments [%s], Apply groups: [%s], except: [%s]' % (
            ', '.join(map(str, self.target)),
            ', '.join(self.comment),
            ', '.join(self.apply_groups),
            ', '.join(self.apply_groups_except),
        )

    def __repr__(self):
        return self.__str__()

    def __eq__(self, obj):
        """Compares for equality against another Header object.

        Note that it is picky and requires the list contents to be in the
        same order.

        Args:
          obj: object to be compared to for equality.
        Returns:
          True if all the list member variables of this object are equal to the list
          member variables of obj and False otherwise.
        """
        if not isinstance(obj, Header):
            return False
        if self.target != obj.target:
            return False
        if self.comment != obj.comment:
            return False
        if self.apply_groups != obj.apply_groups:
            return False
        if self.apply_groups_except != obj.apply_groups_except:
            return False
        return True


# This could be a VarType object, but I'm keeping it as it's class
# b/c we're almost certainly going to have to do something more exotic with
# it shortly to account for various rendering options like default iptables
# policies or output file names, etc. etc.
class Target:
    """The type of acl to be rendered from this policy file."""

    def __init__(self, target):
        self.platform = target[0]
        self.options = target[1:]

    def __str__(self):
        return self.platform

    def __repr__(self):
        return self.__str__()

    def __eq__(self, other):
        return self.platform == other.platform and self.options == other.options

    def __ne__(self, other):
        return not self.__eq__(other)


class TermParseData:
    """TermParseData stores all parsed data verbatim without any processing."""

    def __init__(self, obj: "VarType | CommentExpr"):
        self.name = None
        self.comment = []
        self.data = []
        self.AddObject(obj)

    def AddObject(self, obj: "VarType | CommentExpr"):
        self.data.append(obj)

    def __str__(self):
        result = []
        result.append(f'TermParseData name={self.name}')
        result.extend([f"data   : {data}" for data in self.data])
        return '\n'.join(result)


# Reconstructing comments:
#
# In a .pol file, comments can appear in several places. Pol2yaml should attempt
# to produce an export file with comments positioned to maintain association
# to the extent possible - due to key duplication within terms and headers, this
# cannot be totally exact, unfortunately.
#
# First, an overview of possible comment association scenarios:
#
#
# # BEGINNING OF FILE OR ABOVE HEADER
# header { # BEGIN HEADER (inline)
#   # BEGIN HEADER OR ABOVE KV
#   target:: cisco option # KV Inline
#   # ABOVE KV
#   target:: srx option # KV Inline
#   comment:: comment line1 # KV Inline (comment)
#   comment:: comment line2 # KV Inline (comment)
#   # BOTTOM OF HEADER
# } # BOTTOM OF HEADER (inline)
#
# # ABOVE TERM
# term term-name { # BEGIN TERM (inline)
#   # BEGIN TERM OR ABOVE KV
#   source-address:: NETWORK_NAME # KV Inline
#   # ABOVE KV
#   protocol:: tcp # KV Inline
#   protocol:: udp # KV Inline
#   verbatim:: cisco verbatim_code_for_cisco # KV Inline (verbatim)
#   # BOTTOM OF TERM
# } # BOTTOM OF TERM (inline)
#
# # END OF FILE
#
#
# Now let's look at how that example might be converted to YAML (without comments):
#
#
# filters:
# - header:
#     target:
#       cisco: option
#       srx: option
#     comment: |
#       comment line1
#       comment line2
#   terms:
#   - name: term-name
#     source-address: NETWORK_NAME
#     protocol:
#     - tcp
#     - udp
#     verbatim:
#       cisco: |
#         verbatim_code_for_cisco
#
# A couple of problems should jump out:
# (1) Duplicate key expressions are collapsed
# (2) Comments and verbatim values are expressed as multiline strings
# (3) Some attachments are ambiguous in the first place
#
# In problem (1), .pol allows a key to be repeated within a header or term
# as a way of breaking up values across multiple lines. Contiguity is normal
# but not required.
# Let's look again at the 'target' field and its attachments:
#
#   # BEGIN HEADER OR ABOVE KV (1)
#   target:: cisco option # KV Inline (2)
#   # ABOVE KV (3)
#   target:: srx option # KV Inline (4)
#
# # (1A)
# - header: # (1B)
#     # (1C)
#     target:
#       # (1D)
#       cisco: option # (2)
#       # (3)
#       srx: option # (4)
#
# Comments 2-4 have reasonable attachments that can carry over to YAML. But
# comment 1 is a problem: does it describe the header? The whole list of targets?
# Or just the configuration for cisco? The attachment is unclear. Some possible
# placements are marked in the illustration.
#
# We can start by ruling out slots that are better used for other things. Positions
# 1A and 1B would be used for ABOVE HEADER and BEGIN HEADER (inline). Position 1C
# is physically similar and has the advantage that it _maintains ambiguity_:
# position 1C is still ambiguous as to whether it is describing the header or the list of targets.
# And if we maintain the order of targets in the list, we can still have some proximity to
# the first config (cisco).
#
#
# RULES FOR CONVERTING FILES WITH COMMENTS
#
# 1. A comment appearing on the same line as data should stick to that data.
# 1a. If there are multiple data on the line, stick to the first data point.
# 1b. Multiline strings can't have comments so they have to be collected above the line.
# 2. A comment appearing above a data line should stick to that data.
# 3. A comment appearing at the head of a block should remain at the head
#    of the block.
# 3a. We can look at blank lines (possibly) to distinguish head of block attachments.
# 3b. Blocks with an ambiguous head comment should not have keys re-ordered.
# 4. Comments associated with overwritten data should be left in place.
#


# Problem: We are aggregating AddObject calls in a very raw format
# Problem: that must be processed on a case-by-case basis
# Problem: to correctly export.
#
# Context:
# Each VarType has:
# - A unique calling convention
#   - Single Call
#   - Single Call (list)
#   - Multi Call
#   - Multi Call (list)
#
# PolicyBuilder has some data, but it only describes the mapping from YAML into PolicyModel,
# so it lumps the Multi Call (list) case in with Single Call (list). However we need to
# recieve the call pattern from the parser.


class HeaderParseData:
    """HeaderParseData stores all parsed data verbatim without any processing."""

    def __init__(self):
        self.data = []
        self.comment = []

    def AddObject(self, obj: "tuple[VarType | list[VarType], Optional[str]]"):
        self.data.append(obj)

    def __str__(self):
        result = []
        result.append('HeaderParseData')
        result.extend([f"comment: {comment}" for comment in self.block_comment])
        result.extend([f"data   : {data}" for data in self.data])
        return '\n'.join(result)


# Lexing/Parsing starts here
tokens = (
    'ACTION',
    'ADDR',
    'ADDREXCLUDE',
    'BLOCK_COMMENT',
    'RESTRICT_ADDRESS_FAMILY',
    'COMMENT',
    'COUNTER',
    'DADDR',
    'DADDREXCLUDE',
    'DINTERFACE',
    'DPFX',
    'EDPFX',
    'DPORT',
    'DQUOTEDSTRING',
    'DSCP',
    'DSCP_EXCEPT',
    'DSCP_MATCH',
    'DSCP_RANGE',
    'DSCP_SET',
    'DTAG',
    'DZONE',
    'ENCAPSULATE',
    'ESCAPEDSTRING',
    'ETHER_TYPE',
    'EXPIRATION',
    'FILTER_TERM',
    'FLEXIBLE_MATCH_RANGE',
    'FORWARDING_CLASS',
    'FORWARDING_CLASS_EXCEPT',
    'FRAGMENT_OFFSET',
    'HOP_LIMIT',
    'APPLY_GROUPS',
    'APPLY_GROUPS_EXCEPT',
    'HEADER',
    'HEX',
    'ICMP_TYPE',
    'ICMP_CODE',
    'INTEGER',
    'LOGGING',
    'LOG_LIMIT',
    'LOG_NAME',
    'LOSS_PRIORITY',
    'LPAREN',
    'LSQUARE',
    'NEXT_IP',
    'NEWLINE',
    'OPTION',
    'OWNER',
    'PACKET_LEN',
    'PLATFORM',
    'PLATFORMEXCLUDE',
    'POLICER',
    'PORT',
    'PORT_MIRROR',
    'PRECEDENCE',
    'PRIORITY',
    'PROTOCOL',
    'PROTOCOL_EXCEPT',
    'QOS',
    'RPAREN',
    'RSQUARE',
    'PAN_APPLICATION',
    'ROUTING_INSTANCE',
    'SADDR',
    'SADDREXCLUDE',
    'SINTERFACE',
    'SPFX',
    'ESPFX',
    'SPORT',
    'SZONE',
    'STAG',
    'STRING',
    'TARGET',
    'TARGET_RESOURCES',
    'TARGET_SERVICE_ACCOUNTS',
    'TERM',
    'TIMEOUT',
    'TRAFFIC_CLASS_COUNT',
    'TRAFFIC_TYPE',
    'TTL',
    'VERBATIM',
    'VPN',
)

literals = r':{},-/'
t_ignore = ' \t'
t_LSQUARE = r'\['
t_RSQUARE = r'\]'
t_LPAREN = r'\('
t_RPAREN = r'\)'

reserved = {
    'action': 'ACTION',
    'address': 'ADDR',
    'address-exclude': 'ADDREXCLUDE',
    'restrict-address-family': 'RESTRICT_ADDRESS_FAMILY',
    'comment': 'COMMENT',
    'counter': 'COUNTER',
    'destination-address': 'DADDR',
    'destination-exclude': 'DADDREXCLUDE',
    'destination-interface': 'DINTERFACE',
    'destination-prefix': 'DPFX',
    'destination-prefix-except': 'EDPFX',
    'destination-port': 'DPORT',
    'destination-tag': 'DTAG',
    'destination-zone': 'DZONE',
    'dscp-except': 'DSCP_EXCEPT',
    'dscp-match': 'DSCP_MATCH',
    'dscp-set': 'DSCP_SET',
    'encapsulate': 'ENCAPSULATE',
    'ether-type': 'ETHER_TYPE',
    'expiration': 'EXPIRATION',
    'filter-term': 'FILTER_TERM',
    'flexible-match-range': 'FLEXIBLE_MATCH_RANGE',
    'forwarding-class': 'FORWARDING_CLASS',
    'forwarding-class-except': 'FORWARDING_CLASS_EXCEPT',
    'fragment-offset': 'FRAGMENT_OFFSET',
    'hex': 'HEX',
    'hop-limit': 'HOP_LIMIT',
    'apply-groups': 'APPLY_GROUPS',
    'apply-groups-except': 'APPLY_GROUPS_EXCEPT',
    'header': 'HEADER',
    'icmp-type': 'ICMP_TYPE',
    'icmp-code': 'ICMP_CODE',
    'logging': 'LOGGING',
    'log-limit': 'LOG_LIMIT',
    'log_name': 'LOG_NAME',
    'loss-priority': 'LOSS_PRIORITY',
    'next-ip': 'NEXT_IP',
    'option': 'OPTION',
    'owner': 'OWNER',
    'packet-length': 'PACKET_LEN',
    'platform': 'PLATFORM',
    'platform-exclude': 'PLATFORMEXCLUDE',
    'policer': 'POLICER',
    'port': 'PORT',
    'port-mirror': 'PORT_MIRROR',
    'precedence': 'PRECEDENCE',
    'priority': 'PRIORITY',
    'protocol': 'PROTOCOL',
    'protocol-except': 'PROTOCOL_EXCEPT',
    'qos': 'QOS',
    'pan-application': 'PAN_APPLICATION',
    'routing-instance': 'ROUTING_INSTANCE',
    'source-address': 'SADDR',
    'source-exclude': 'SADDREXCLUDE',
    'source-interface': 'SINTERFACE',
    'source-prefix': 'SPFX',
    'source-prefix-except': 'ESPFX',
    'source-port': 'SPORT',
    'source-tag': 'STAG',
    'source-zone': 'SZONE',
    'target': 'TARGET',
    'target-resources': 'TARGET_RESOURCES',
    'target-service-accounts': 'TARGET_SERVICE_ACCOUNTS',
    'term': 'TERM',
    'timeout': 'TIMEOUT',
    'traffic-class-count': 'TRAFFIC_CLASS_COUNT',
    'traffic-type': 'TRAFFIC_TYPE',
    'ttl': 'TTL',
    'verbatim': 'VERBATIM',
    'vpn': 'VPN',
}

# disable linting warnings for lexx/yacc code
# pylint: disable=unused-argument,invalid-name,g-short-docstring-punctuation
# pylint: disable=g-docstring-quotes,g-short-docstring-space
# pylint: disable=g-space-before-docstring-summary,g-doc-args
# pylint: disable=g-no-space-after-docstring-summary
# pylint: disable=g-docstring-missing-newline


def t_BLOCK_COMMENT(t):
    r'\#.*'
    t.lexer.lineno += str(t.value).count('\n')
    return t


def t_ESCAPEDSTRING(t):
    r'"([^"\\]*(?:\\"[^"\\]*)+)"'
    t.lexer.lineno += str(t.value).count('\n')
    return t


def t_DQUOTEDSTRING(t):
    r'"[^"]*?"'
    t.lexer.lineno += str(t.value).count('\n')
    return t


def t_NEWLINE(t):
    r'\n+'
    t.lexer.lineno += len(t.value)
    return t


def t_error(t):
    print("Illegal character '%s' on line %s" % (t.value[0], t.lineno))
    t.lexer.skip(1)


def t_DSCP_RANGE(t):
    # pylint: disable=line-too-long
    r'\b((b[0-1]{6})|(af[1-4]{1}[1-3]{1})|(be)|(ef)|(cs[0-7]{1}))([-]{1})((b[0-1]{6})|(af[1-4]{1}[1-3]{1})|(be)|(ef)|(cs[0-7]{1}))\b'
    t.type = reserved.get(t.value, 'DSCP_RANGE')
    return t


def t_DSCP(t):
    # we need to handle the '-' as part of the word, not as a boundary
    r'\b((b[0-1]{6})|(af[1-4]{1}[1-3]{1})|(be)|(ef)|(cs[0-7]{1}))(?![\w-])\b'
    t.type = reserved.get(t.value, 'DSCP')
    return t


def t_HEX(t):
    r'0x[a-fA-F0-9]+'
    return t


def t_INTEGER(t):
    r'\d+'
    return t


def t_STRING(t):
    r'\w+([-_+.@/]\w*)*'
    # we have an identifier; let's check if it's a keyword or just a string.
    t.type = reserved.get(t.value, 'STRING')
    return t


###
## parser starts here
###
def p_file(p):
    """file : o target"""
    breakpoint()
    if _PRESERVE_ORIGINAL and isinstance(p[2], PolicyParseData) and isinstance(p[1], CommentExpr):
        p[2].AddBlockComment(p[1])
    p[0] = p[2]


def p_target(p):
    """target : target header terms o
    |"""
    breakpoint()
    if _PRESERVE_ORIGINAL and len(p) > 1:
        if not isinstance(p[1], PolicyParseData):
            p[1] = PolicyParseData(p[2], p[3], p[4])
        else:
            p[1].AddFilter(p[2], p[3], p[4])
        p[0] = p[1]

    elif len(p) > 1:
        if not isinstance(p[1], Policy):
            p[1] = Policy(p[2], p[3])
        else:
            p[1].AddFilter(p[2], p[3])
        p[0] = p[1]


def p_header(p):
    """header : HEADER o '{' lc o header_spec '}'"""
    breakpoint()
    if _PRESERVE_ORIGINAL:
        if isinstance(p[2], CommentExpr):
            p[6].comment.append(p[2])
        if isinstance(p[4], CommentExpr):
            p[6].comment.append(p[4])
        # Treat p[5] as an inter-field comment
        if isinstance(p[5], CommentExpr):
            p[6].AddObject(p[5])
    else:
        p[0] = p[6]


def p_header_spec(p):
    """header_spec : header_spec target_spec
    | header_spec comment_spec
    | header_spec apply_groups_spec
    | header_spec apply_groups_except_spec
    |"""
    breakpoint()
    if _PRESERVE_ORIGINAL and len(p) > 1:
        if not isinstance(p[1], HeaderParseData):
            p[1] = HeaderParseData()
        p[1].AddObject(p[2])
        p[0] = p[1]
    elif len(p) > 1:
        if not isinstance(p[1], Header):
            p[1] = Header()
        p[1].AddObject(p[2])
        p[0] = p[1]


# we may want to change this at some point if we want to be clever with things
# like being able to set a default input/output policy for iptables policies.
def p_target_spec(p):
    """target_spec : TARGET o ':' o ':' lc o strings_or_ints"""
    # breakpoint()
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        p[0] = VarType(VarType.TARGET, p[8], field_comment=c)
    else:
        p[0] = Target(p[8])


def p_terms(p):
    """terms : terms o TERM o STRING o '{' lc o term_spec '}'
    |"""
    # breakpoint()
    if _PRESERVE_ORIGINAL and len(p) > 1:
        if not isinstance(p[1], list):
            p[1] = []

        # Treat p[2] as an inter-term comment
        if isinstance(p[2], CommentExpr):
            p[1].append(p[2])

        # Name-line comments
        if isinstance(p[4], CommentExpr):
            p[10].comment.append(p[4])

        p[10].name = p[5]

        if isinstance(p[6], CommentExpr):
            p[10].comment.append(p[6])
        if isinstance(p[8], CommentExpr):
            p[10].comment.append(p[8])

        # Treat p[9] as an inter-field comment
        if isinstance(p[9], CommentExpr):
            p[10].AddObject(p[9])

        p[1].append(p[10])
        p[0] = p[1]

    elif len(p) > 1:
        if not isinstance(p[1], list):
            p[1] = []
        p[10].name = p[5]
        p[1].append(p[10])
        p[0] = p[1]


def p_term_spec(p):
    """term_spec : term_spec action_spec
    | term_spec addr_spec
    | term_spec restrict_address_family_spec
    | term_spec comment_spec
    | term_spec counter_spec
    | term_spec traffic_class_count_spec
    | term_spec dscp_set_spec
    | term_spec dscp_match_spec
    | term_spec dscp_except_spec
    | term_spec encapsulate_spec
    | term_spec ether_type_spec
    | term_spec exclude_spec
    | term_spec expiration_spec
    | term_spec filter_term_spec
    | term_spec flexible_match_range_spec
    | term_spec forwarding_class_spec
    | term_spec forwarding_class_except_spec
    | term_spec fragment_offset_spec
    | term_spec hop_limit_spec
    | term_spec icmp_type_spec
    | term_spec icmp_code_spec
    | term_spec interface_spec
    | term_spec logging_spec
    | term_spec log_limit_spec
    | term_spec log_name_spec
    | term_spec losspriority_spec
    | term_spec next_ip_spec
    | term_spec option_spec
    | term_spec owner_spec
    | term_spec packet_length_spec
    | term_spec platform_spec
    | term_spec policer_spec
    | term_spec port_spec
    | term_spec port_mirror_spec
    | term_spec precedence_spec
    | term_spec priority_spec
    | term_spec prefix_list_spec
    | term_spec protocol_spec
    | term_spec qos_spec
    | term_spec pan_application_spec
    | term_spec routinginstance_spec
    | term_spec term_zone_spec
    | term_spec tag_list_spec
    | term_spec target_resources_spec
    | term_spec target_service_accounts_spec
    | term_spec timeout_spec
    | term_spec ttl_spec
    | term_spec traffic_type_spec
    | term_spec verbatim_spec
    | term_spec vpn_spec
    |"""
    if _PRESERVE_ORIGINAL and len(p) > 1:

        def AddObject(obj):
            if isinstance(p[1], TermParseData):
                p[1].AddObject(obj)
            else:
                p[1] = TermParseData(obj)

        AddObject(p[2])
        p[0] = p[1]

    elif len(p) > 1:
        if isinstance(p[1], Term):
            p[1].AddObject(p[2])
            p[0] = p[1]
        else:
            p[0] = Term(p[2])


def p_restrict_address_family_spec(p):
    """restrict_address_family_spec : RESTRICT_ADDRESS_FAMILY o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.RESTRICT_ADDRESS_FAMILY, v, field_comment=c)
    else:
        p[0] = VarType(VarType.RESTRICT_ADDRESS_FAMILY, p[8])


def p_routinginstance_spec(p):
    """routinginstance_spec : ROUTING_INSTANCE o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.ROUTING_INSTANCE, v, field_comment=c)
    else:
        p[0] = VarType(VarType.ROUTING_INSTANCE, p[8])


def p_losspriority_spec(p):
    """losspriority_spec : LOSS_PRIORITY o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.LOSS_PRIORITY, v, field_comment=c)
    else:
        p[0] = VarType(VarType.LOSS_PRIORITY, p[8])


def p_precedence_spec(p):
    """precedence_spec : PRECEDENCE o ':' o ':' lc o one_or_more_ints"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        p[0] = VarType(VarType.PRECEDENCE, p[8], field_comment=c)
    else:
        p[0] = VarType(VarType.PRECEDENCE, p[8])


def p_flexible_match_range_spec(p):
    """flexible_match_range_spec : FLEXIBLE_MATCH_RANGE o ':' o ':' lc o flex_match_key_values"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.FLEXIBLE_MATCH_RANGE, p[8], field_comment=c)
    else:
        p[0] = []
        for kv in p[8]:
            p[0].append(VarType(VarType.FLEXIBLE_MATCH_RANGE, kv))


def p_flex_match_key_values(p):
    """flex_match_key_values : flex_match_key_values STRING lc o HEX lc o
    | flex_match_key_values STRING lc o INTEGER lc o
    | flex_match_key_values STRING lc o STRING lc o
    |"""
    if len(p) < 2:
        return

    if p[2] not in FLEXIBLE_MATCH_RANGE_ATTRIBUTES:
        raise FlexibleMatchError('%s is not a valid attribute' % p[2])
    if p[2] == 'match-start':
        if p[5] not in FLEXIBLE_MATCH_START_OPTIONS:
            raise FlexibleMatchError('%s value is not valid' % p[2])
    # per Juniper, max bit length is 32
    elif p[2] == 'bit-length':
        if int(p[5]) not in list(range(33)):
            raise FlexibleMatchError('%s value is not valid' % p[2])
    # per Juniper, max bit offset is 7
    elif p[2] == 'bit-offset':
        if int(p[5]) not in list(range(8)):
            raise FlexibleMatchError('%s value is not valid' % p[2])
    # per Juniper, offset can be up to 256 bytes
    elif p[2] == 'byte-offset':
        if int(p[5]) not in list(range(256)):
            raise FlexibleMatchError('%s value is not valid' % p[2])

    if _PRESERVE_ORIGINAL:
        if type(p[1]) == list:
            v = p[1]
        else:
            v = []
        v.append(ValueExpr(p[2], p[3]))
        if isinstance(p[4], CommentExpr):
            v.append(p[4])
        v.append(ValueExpr(p[5], p[6]))
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        p[0] = v
    else:
        if type(p[1]) == list:
            p[1].append([p[2], p[5]])
            p[0] = p[1]
        else:
            p[0] = [[p[2], p[5]]]


def p_forwarding_class_spec(p):
    """forwarding_class_spec : FORWARDING_CLASS o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.FORWARDING_CLASS, p[8], field_comment=c)
    else:
        p[0] = []
        for fclass in p[8]:
            p[0].append(VarType(VarType.FORWARDING_CLASS, fclass))


def p_forwarding_class_except_spec(p):
    """forwarding_class_except_spec : FORWARDING_CLASS_EXCEPT o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.FORWARDING_CLASS_EXCEPT, p[8], field_comment=c)
    else:
        p[0] = []
        for fclass in p[8]:
            p[0].append(VarType(VarType.FORWARDING_CLASS_EXCEPT, fclass))


def p_next_ip_spec(p):
    """next_ip_spec : NEXT_IP o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.NEXT_IP, v, field_comment=c)
    else:
        p[0] = VarType(VarType.NEXT_IP, p[8])


def p_encapsulate_spec(p):
    """encapsulate_spec : ENCAPSULATE o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.ENCAPSULATE, v, field_comment=c)
    else:
        p[0] = VarType(VarType.ENCAPSULATE, p[8])


def p_port_mirror_spec(p):
    """port_mirror_spec : PORT_MIRROR o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.PORT_MIRROR, v, field_comment=c)
    else:
        p[0] = VarType(VarType.PORT_MIRROR, p[8])


def p_icmp_type_spec(p):
    """icmp_type_spec : ICMP_TYPE o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        p[0] = VarType(VarType.ICMP_TYPE, p[8], field_comment=c)
    else:
        p[0] = VarType(VarType.ICMP_TYPE, p[8])


def p_icmp_code_spec(p):
    """icmp_code_spec : ICMP_CODE o ':' o ':' lc o one_or_more_ints"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        p[0] = VarType(VarType.ICMP_CODE, p[8], field_comment=c)
    else:
        p[0] = VarType(VarType.ICMP_CODE, p[8])


def p_priority_spec(p):
    """priority_spec : PRIORITY o ':' o ':' lc o INTEGER lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.PRIORITY, v, field_comment=c)
    else:
        p[0] = VarType(VarType.PRIORITY, p[8])


def p_packet_length_spec(p):
    """packet_length_spec : PACKET_LEN o ':' o ':' lc o INTEGER lc o
    | PACKET_LEN o ':' o ':' lc o INTEGER lc o '-' o INTEGER lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        if len(p) > 11:
            if isinstance(p[12], CommentExpr):
                v.append(p[12])
            v.append(ValueExpr(p[13], p[14]))
            if isinstance(p[15], CommentExpr):
                v.append(p[15])
        p[0] = VarType(VarType.PACKET_LEN, v, field_comment=c)
    else:
        if len(p) == 11:
            p[0] = VarType(VarType.PACKET_LEN, str(p[8]))
        else:
            p[0] = VarType(VarType.PACKET_LEN, str(p[8]) + '-' + str(p[15]))


def p_fragment_offset_spec(p):
    """fragment_offset_spec : FRAGMENT_OFFSET o ':' o ':' lc o INTEGER lc o
    | FRAGMENT_OFFSET o ':' o ':' lc o INTEGER lc o '-' o INTEGER lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        if len(p) > 11:
            if isinstance(p[12], CommentExpr):
                v.append(p[12])
            v.append(ValueExpr(p[13], p[14]))
            if isinstance(p[15], CommentExpr):
                v.append(p[15])
        p[0] = VarType(VarType.FRAGMENT_OFFSET, v, field_comment=c)
    else:
        if len(p) == 11:
            p[0] = VarType(VarType.FRAGMENT_OFFSET, str(p[8]))
        else:
            p[0] = VarType(VarType.FRAGMENT_OFFSET, str(p[8]) + '-' + str(p[13]))


def p_hop_limit_spec(p):
    """hop_limit_spec : HOP_LIMIT o ':' o ':' lc o INTEGER lc o
    | HOP_LIMIT o ':' o ':' lc o INTEGER lc o '-' o INTEGER lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        if len(p) > 11:
            if isinstance(p[12], CommentExpr):
                v.append(p[12])
            v.append(ValueExpr(p[13], p[14]))
            if isinstance(p[15], CommentExpr):
                v.append(p[15])
        p[0] = VarType(VarType.HOP_LIMIT, v, field_comment=c)
    else:
        if len(p) == 11:
            p[0] = VarType(VarType.HOP_LIMIT, str(p[8]))
        else:
            p[0] = VarType(VarType.HOP_LIMIT, str(p[8]) + '-' + str(p[13]))


def p_one_or_more_dscps(p):
    """one_or_more_dscps : one_or_more_dscps DSCP_RANGE lc o
    | one_or_more_dscps DSCP lc o
    | one_or_more_dscps INTEGER lc o
    | DSCP_RANGE lc o
    | DSCP lc o
    | INTEGER lc o"""
    if _PRESERVE_ORIGINAL:
        if len(p) > 4:
            value = ValueExpr(p[2], p[3])
            if type(p[1]) != list:
                p[1] = []
            p[1].append(value)
            if isinstance(p[4], CommentExpr):
                p[1].append(p[4])
            p[0] = p[1]
        else:
            p[0] = [ValueExpr(p[1], p[2])]
            if isinstance(p[3], CommentExpr):
                p[0].append(p[3])
    if len(p) > 1:
        if isinstance(p[1], list):
            p[1].append(p[2])
            p[0] = p[1]
        else:
            p[0] = [p[1]]


def p_dscp_set_spec(p):
    """dscp_set_spec : DSCP_SET o ':' o ':' lc o DSCP lc o
    | DSCP_SET o ':' o ':' lc o INTEGER lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.DSCP_SET, v, field_comment=c)
    else:
        p[0] = VarType(VarType.DSCP_SET, p[8])


def p_dscp_match_spec(p):
    """dscp_match_spec : DSCP_MATCH o ':' o ':' lc o one_or_more_dscps"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.DSCP_MATCH, p[8], field_comment=c)
    else:
        p[0] = []
        for dscp in p[8]:
            p[0].append(VarType(VarType.DSCP_MATCH, dscp))


def p_dscp_except_spec(p):
    """dscp_except_spec : DSCP_EXCEPT o ':' o ':' lc o one_or_more_dscps"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.DSCP_EXCEPT, p[8], field_comment=c)
    else:
        p[0] = []
        for dscp in p[8]:
            p[0].append(VarType(VarType.DSCP_EXCEPT, dscp))


def p_exclude_spec(p):
    """exclude_spec : SADDREXCLUDE o ':' o ':' lc o one_or_more_strings
    | DADDREXCLUDE o ':' o ':' lc o one_or_more_strings
    | ADDREXCLUDE o ':' o ':' lc o one_or_more_strings
    | PROTOCOL_EXCEPT o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        if p[1].find('source-exclude') >= 0:
            p[0] = VarType(VarType.SADDREXCLUDE, p[8], field_comment=c)
        elif p[1].find('destination-exclude') >= 0:
            p[0] = VarType(VarType.DADDREXCLUDE, p[8], field_comment=c)
        elif p[1].find('address-exclude') >= 0:
            p[0] = VarType(VarType.ADDREXCLUDE, p[8], field_comment=c)
        elif p[1].find('protocol-except') >= 0:
            p[0] = VarType(VarType.PROTOCOL_EXCEPT, p[8], field_comment=c)
    else:
        p[0] = []
        for ex in p[8]:
            if p[1].find('source-exclude') >= 0:
                p[0].append(VarType(VarType.SADDREXCLUDE, ex))
            elif p[1].find('destination-exclude') >= 0:
                p[0].append(VarType(VarType.DADDREXCLUDE, ex))
            elif p[1].find('address-exclude') >= 0:
                p[0].append(VarType(VarType.ADDREXCLUDE, ex))
            elif p[1].find('protocol-except') >= 0:
                p[0].append(VarType(VarType.PROTOCOL_EXCEPT, ex))


def p_prefix_list_spec(p):
    """prefix_list_spec : DPFX o ':' o ':' lc o one_or_more_strings
    | EDPFX o ':' o ':' lc o one_or_more_strings
    | SPFX o ':' o ':' lc o one_or_more_strings
    | ESPFX o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        if p[1].find('source-prefix-except') >= 0:
            p[0] = VarType(VarType.ESPFX, p[8], field_comment=c)
        elif p[1].find('source-prefix') >= 0:
            p[0] = VarType(VarType.SPFX, p[8], field_comment=c)
        elif p[1].find('destination-prefix-except') >= 0:
            p[0] = VarType(VarType.EDPFX, p[8], field_comment=c)
        elif p[1].find('destination-prefix') >= 0:
            p[0] = VarType(VarType.DPFX, p[8], field_comment=c)
    else:
        p[0] = []
        for pfx in p[8]:
            if p[1].find('source-prefix-except') >= 0:
                p[0].append(VarType(VarType.ESPFX, pfx))
            elif p[1].find('source-prefix') >= 0:
                p[0].append(VarType(VarType.SPFX, pfx))
            elif p[1].find('destination-prefix-except') >= 0:
                p[0].append(VarType(VarType.EDPFX, pfx))
            elif p[1].find('destination-prefix') >= 0:
                p[0].append(VarType(VarType.DPFX, pfx))


def p_addr_spec(p):
    """addr_spec : SADDR o ':' o ':' lc o one_or_more_strings
    | DADDR o ':' o ':' lc o one_or_more_strings
    | ADDR o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        if p[1].find('source-address') >= 0:
            p[0] = VarType(VarType.SADDRESS, p[8], field_comment=c)
        elif p[1].find('destination-address') >= 0:
            p[0] = VarType(VarType.DADDRESS, p[8], field_comment=c)
        else:
            p[0] = VarType(VarType.ADDRESS, p[8], field_comment=c)
    else:
        p[0] = []
        for addr in p[8]:
            if p[1].find('source-address') >= 0:
                p[0].append(VarType(VarType.SADDRESS, addr))
            elif p[1].find('destination-address') >= 0:
                p[0].append(VarType(VarType.DADDRESS, addr))
            else:
                p[0].append(VarType(VarType.ADDRESS, addr))


def p_port_spec(p):
    """port_spec : SPORT o ':' o ':' lc o one_or_more_strings
    | DPORT o ':' o ':' lc o one_or_more_strings
    | PORT o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        if p[1].find('source-port') >= 0:
            p[0] = VarType(VarType.SPORT, p[8], field_comment=c)
        elif p[1].find('destination-port') >= 0:
            p[0] = VarType(VarType.DPORT, p[8], field_comment=c)
        else:
            p[0] = VarType(VarType.PORT, p[8], field_comment=c)
    else:
        p[0] = []
        for port in p[8]:
            if p[1].find('source-port') >= 0:
                p[0].append(VarType(VarType.SPORT, port))
            elif p[1].find('destination-port') >= 0:
                p[0].append(VarType(VarType.DPORT, port))
            else:
                p[0].append(VarType(VarType.PORT, port))


def p_protocol_spec(p):
    """protocol_spec : PROTOCOL o ':' o ':' lc o strings_or_ints"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.PROTOCOL, p[8], field_comment=c)
    else:
        p[0] = []
        for proto in p[8]:
            p[0].append(VarType(VarType.PROTOCOL, proto))


def p_tag_list_spec(p):
    """tag_list_spec : DTAG o ':' o ':' lc o one_or_more_strings
    | STAG o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        if p[1].find('source-tag') >= 0:
            p[0] = VarType(VarType.STAG, p[8], field_comment=c)
        elif p[1].find('destination-tag') >= 0:
            p[0] = VarType(VarType.DTAG, p[8], field_comment=c)
    else:
        p[0] = []
        for tag in p[8]:
            if p[1].find('source-tag') >= 0:
                p[0].append(VarType(VarType.STAG, tag))
            elif p[1].find('destination-tag') >= 0:
                p[0].append(VarType(VarType.DTAG, tag))


def p_target_resources_spec(p):
    """target_resources_spec : TARGET_RESOURCES o ':' o ':' lc o one_or_more_tuples"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.TARGET_RESOURCES, p[8], field_comment=c)
    else:
        p[0] = []
        for target_resource in p[8]:
            p[0].append(VarType(VarType.TARGET_RESOURCES, target_resource))


def p_target_service_accounts_spec(p):
    """target_service_accounts_spec : TARGET_SERVICE_ACCOUNTS o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.TARGET_SERVICE_ACCOUNTS, p[8], field_comment=c)
    else:
        p[0] = []
        for service_account in p[8]:
            p[0].append(VarType(VarType.TARGET_SERVICE_ACCOUNTS, service_account))


def p_ether_type_spec(p):
    """ether_type_spec : ETHER_TYPE o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.ETHER_TYPE, p[8], field_comment=c)
    else:
        p[0] = []
        for proto in p[8]:
            p[0].append(VarType(VarType.ETHER_TYPE, proto))


def p_traffic_type_spec(p):
    """traffic_type_spec : TRAFFIC_TYPE o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.TRAFFIC_TYPE, p[8], field_comment=c)
    else:
        p[0] = []
        for proto in p[8]:
            p[0].append(VarType(VarType.TRAFFIC_TYPE, proto))


def p_policer_spec(p):
    """policer_spec : POLICER o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.POLICER, v, field_comment=c)
    else:
        p[0] = VarType(VarType.POLICER, p[8])


def p_logging_spec(p):
    """logging_spec : LOGGING o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.LOGGING, v, field_comment=c)
    else:
        p[0] = VarType(VarType.LOGGING, p[8])


def p_log_limit_spec(p):
    """log_limit_spec : LOG_LIMIT o ':' o ':' lc o INTEGER lc o '/' o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        if isinstance(p[12], CommentExpr):
            v.append(p[12])
        v.append(ValueExpr(p[13], p[14]))
        if isinstance(p[15], CommentExpr):
            v.append(p[15])
        p[0] = VarType(VarType.LOG_LIMIT, v, field_comment=c)
    else:
        p[0] = VarType(VarType.LOG_LIMIT, (p[8], p[13]))


def p_log_name_spec(p):
    """log_name_spec : LOG_NAME o ':' o ':' lc o DQUOTEDSTRING lc"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.LOG_NAME, v, field_comment=c)
    else:
        p[0] = VarType(VarType.LOG_NAME, p[8])


def p_option_spec(p):
    """option_spec : OPTION o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.OPTION, p[8], field_comment=c)
    else:
        p[0] = []
        for opt in p[8]:
            p[0].append(VarType(VarType.OPTION, opt))


def p_action_spec(p):
    """action_spec : ACTION o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.ACTION, v, field_comment=c)
    else:
        p[0] = VarType(VarType.ACTION, p[8])


def p_counter_spec(p):
    """counter_spec : COUNTER o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.COUNTER, v, field_comment=c)
    else:
        p[0] = VarType(VarType.COUNTER, p[8])


def p_traffic_class_count_spec(p):
    """traffic_class_count_spec : TRAFFIC_CLASS_COUNT o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.TRAFFIC_CLASS_COUNT, v, field_comment=c)
    else:
        p[0] = VarType(VarType.TRAFFIC_CLASS_COUNT, p[8])


def p_expiration_spec(p):
    """expiration_spec : EXPIRATION o ':' o ':' lc o INTEGER lc o '-' o INTEGER lc o '-' o INTEGER lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(int(p[8]), p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        if isinstance(p[12], CommentExpr):
            v.append(p[12])
        v.append(ValueExpr(int(p[13]), p[14]))
        if isinstance(p[15], CommentExpr):
            v.append(p[15])
        if isinstance(p[17], CommentExpr):
            v.append(p[17])
        v.append(ValueExpr(int(p[18]), p[19]))
        if isinstance(p[20], CommentExpr):
            v.append(p[20])
        p[0] = VarType(VarType.EXPIRATION, v, field_comment=c)
    else:
        p[0] = VarType(VarType.EXPIRATION, datetime.date(int(p[8]), int(p[13]), int(p[18])))


def p_comment_spec(p):
    """comment_spec : COMMENT o ':' o ':' lc o DQUOTEDSTRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.COMMENT, v, field_comment=c)
    else:
        p[0] = VarType(VarType.COMMENT, p[8])


def p_owner_spec(p):
    """owner_spec : OWNER o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.OWNER, v, field_comment=c)
    else:
        p[0] = VarType(VarType.OWNER, p[8])


def p_verbatim_spec(p):
    """verbatim_spec : VERBATIM o ':' o ':' lc o STRING lc o DQUOTEDSTRING lc o
    | VERBATIM o ':' o ':' lc o STRING lc o ESCAPEDSTRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        v.append(ValueExpr(p[11].strip('"').replace('\\"', '"'), p[12]))
        if isinstance(p[13], CommentExpr):
            v.append(p[13])
        p[0] = VarType(VarType.VERBATIM, v, field_comment=c)
    else:
        p[0] = VarType(VarType.VERBATIM, [p[8], p[11].strip('"').replace('\\"', '"')])


def p_term_zone_spec(p):
    """term_zone_spec : SZONE o ':' o ':' lc o one_or_more_strings
    | DZONE o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        if p[1].find('source-zone') >= 0:
            p[0] = VarType(VarType.SZONE, p[8], field_comment=c)
        elif p[1].find('destination-zone') >= 0:
            p[0] = VarType(VarType.DZONE, p[8], field_comment=c)
    else:
        p[0] = []
        for zone in p[8]:
            if p[1].find('source-zone') >= 0:
                p[0].append(VarType(VarType.SZONE, zone))
            elif p[1].find('destination-zone') >= 0:
                p[0].append(VarType(VarType.DZONE, zone))


def p_vpn_spec(p):
    """vpn_spec : VPN o ':' o ':' lc o STRING lc o STRING lc o
    | VPN o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        if len(p) > 11:
            v.append(ValueExpr(p[11], p[12]))
            if isinstance(p[13], CommentExpr):
                v.append(p[13])
        p[0] = VarType(VarType.VPN, v, field_comment=c)
    else:
        if len(p) > 11:
            p[0] = VarType(VarType.VPN, [p[8], p[11]])
        else:
            p[0] = VarType(VarType.VPN, [p[8], ''])


def p_qos_spec(p):
    """qos_spec : QOS o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        p[0] = VarType(VarType.QOS, v, field_comment=c)
    else:
        p[0] = VarType(VarType.QOS, p[8])


def p_pan_application_spec(p):
    """pan_application_spec : PAN_APPLICATION o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.PAN_APPLICATION, p[8], field_comment=c)
    else:
        p[0] = []
        for apps in p[8]:
            p[0].append(VarType(VarType.PAN_APPLICATION, apps))


def p_interface_spec(p):
    """interface_spec : SINTERFACE o ':' o ':' lc o STRING lc o
    | DINTERFACE o ':' o ':' lc o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        v = []
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        v.append(ValueExpr(p[8], p[9]))
        if isinstance(p[10], CommentExpr):
            v.append(p[10])
        if p[1].find('source-interface') >= 0:
            p[0] = VarType(VarType.SINTERFACE, v, field_comment=c)
        elif p[1].find('destination-interface') >= 0:
            p[0] = VarType(VarType.DINTERFACE, v, field_comment=c)
    else:
        if p[1].find('source-interface') >= 0:
            p[0] = VarType(VarType.SINTERFACE, p[8])
        elif p[1].find('destination-interface') >= 0:
            p[0] = VarType(VarType.DINTERFACE, p[8])


def p_platform_spec(p):
    """platform_spec : PLATFORM o ':' o ':' lc o one_or_more_strings
    | PLATFORMEXCLUDE o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        if p[1].find('platform-exclude') >= 0:
            p[0] = VarType(VarType.PLATFORMEXCLUDE, p[8], field_comment=c)
        elif p[1].find('platform') >= 0:
            p[0] = VarType(VarType.PLATFORM, p[8], field_comment=c)
    else:
        p[0] = []
        for platform in p[8]:
            if p[1].find('platform-exclude') >= 0:
                p[0].append(VarType(VarType.PLATFORMEXCLUDE, platform))
            elif p[1].find('platform') >= 0:
                p[0].append(VarType(VarType.PLATFORM, platform))


def p_apply_groups_spec(p):
    """apply_groups_spec : APPLY_GROUPS o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.APPLY_GROUPS, p[8], field_comment=c)
    else:
        p[0] = []
        for group in p[8]:
            p[0].append(VarType(VarType.APPLY_GROUPS, group))


def p_apply_groups_except_spec(p):
    """apply_groups_except_spec : APPLY_GROUPS_EXCEPT o ':' o ':' lc o one_or_more_strings"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        # Distinguish field line from inter-value comment
        if not p[8]:
            p[8] = []
        if isinstance(p[7], CommentExpr):
            p[8].insert(0, p[7])
        # NOTE: using one VarType per field line
        p[0] = VarType(VarType.APPLY_GROUPS_EXCEPT, p[8], field_comment=c)
    else:
        p[0] = []
        for group_except in p[8]:
            p[0].append(VarType(VarType.APPLY_GROUPS_EXCEPT, group_except))


def p_timeout_spec(p):
    """timeout_spec : TIMEOUT o ':' o ':' o INTEGER lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        p[0] = VarType(VarType.TIMEOUT, ValueExpr(p[7], p[8]), field_comment=c)
        if isinstance(p[9], CommentExpr):
            c.append(p[9])
    else:
        p[0] = VarType(VarType.TIMEOUT, p[7])


def p_ttl_spec(p):
    """ttl_spec : TTL o ':' o ':' o INTEGER lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        p[0] = VarType(VarType.TTL, ValueExpr(p[7], p[8]), field_comment=c)
        if isinstance(p[9], CommentExpr):
            c.append(p[9])
    else:
        p[0] = VarType(VarType.TTL, p[7])


def p_filter_term_spec(p):
    """filter_term_spec : FILTER_TERM o ':' o ':' o STRING lc o"""
    if _PRESERVE_ORIGINAL:
        c = []
        if isinstance(p[2], CommentExpr):
            c.append(p[2])
        if isinstance(p[4], CommentExpr):
            c.append(p[4])
        if isinstance(p[6], CommentExpr):
            c.append(p[6])
        p[0] = VarType(VarType.FILTER_TERM, ValueExpr(p[7], p[8]), field_comment=c)
        if isinstance(p[9], CommentExpr):
            c.append(p[9])
    else:
        p[0] = VarType(VarType.FILTER_TERM, p[7])


def p_one_or_more_strings(p):
    """one_or_more_strings : one_or_more_strings STRING lc o
    |"""
    if _PRESERVE_ORIGINAL and len(p) > 1:
        if not isinstance(p[1], list):
            p[1] = []
        p[1].append(ValueExpr(p[2], p[3]))
        if isinstance(p[4], CommentExpr):
            p[1].append(p[4])
        p[0] = p[1]
    elif len(p) > 1:
        if not isinstance(p[1], list):
            p[1] = []
        p[1].append(p[2])
        p[0] = p[1]


def p_one_or_more_tuples(p):
    """one_or_more_tuples : LSQUARE o one_or_more_tuples RSQUARE o
    | one_or_more_tuples ',' o one_tuple o
    | one_or_more_tuples one_tuple o
    |"""
    if _PRESERVE_ORIGINAL and len(p) > 1:
        if p[1] == '[':
            if type(p[3]) != list:
                p[3] = []
            if isinstance(p[2], CommentExpr):
                p[3].insert(0, p[2])
            # TODO a line comment following ] can be attached to
            # the line comment expression of the RHS of the rightmost tuple
            # e.g. ]  # line comment
            if isinstance(p[5], CommentExpr):
                p[3].append(p[5])
            p[0] = p[3]
        else:
            if type(p[1]) != list:
                p[1] = []

            if p[2] == ',':
                if isinstance(p[3], CommentExpr):
                    p[1].append(p[3])
                p[1].append(p[4])
                if isinstance(p[5], CommentExpr):
                    p[1].append(p[5])
            else:
                p[1].append(p[2])
                if isinstance(p[3], CommentExpr):
                    p[1].append(p[3])

            p[0] = p[1]
    elif len(p) > 1:
        if p[1] == '[':
            p[0] = p[3]
        else:
            if not isinstance(p[1], list):
                p[1] = []
            if p[2] == ',':
                p[1].append(p[4])
            else:
                p[1].append(p[2])
            p[0] = p[1]


def p_one_tuple(p):
    """one_tuple : LPAREN o STRING lc o ',' o STRING lc o RPAREN lc"""
    # NOTE - cleaned up a grammar bug here where one_tuple -> <empty> caused r/r conflicts
    if _PRESERVE_ORIGINAL:
        v = []
        if isinstance(p[2], CommentExpr):
            v.append(p[2])
        v.append(ValueExpr(p[3], p[4]))
        if isinstance(p[5], CommentExpr):
            v.append(p[5])
        if isinstance(p[7], CommentExpr):
            v.append(p[7])
        value_rhs = ValueExpr(p[8], p[9])
        v.append(value_rhs)
        # Special case - treat comments before and immediately following RPAREN as
        # RH value line comments
        if isinstance(p[10], CommentExpr):
            value_rhs.comment.extend(p[10])
        if isinstance(p[12], CommentExpr):
            value_rhs.comment.extend(p[12])
        p[0] = v
    else:
        p[0] = (p[3], p[8])


def p_one_or_more_ints(p):
    """one_or_more_ints : one_or_more_ints INTEGER lc o
    |"""
    if _PRESERVE_ORIGINAL and len(p) > 1:
        if type(p[1]) != list:
            p[1] = []
        p[1].append(ValueExpr(int(p[2]), p[3]))
        if isinstance(p[4], CommentExpr):
            p[1].append(p[4])
        p[0] = p[1]
    elif len(p) > 1:
        if isinstance(p[1], list):
            p[1].append(int(p[2]))
            p[0] = p[1]
        else:
            p[0] = [int(p[2])]


def p_strings_or_ints(p):
    """strings_or_ints : strings_or_ints STRING lc o
    | strings_or_ints INTEGER lc o
    |"""
    if _PRESERVE_ORIGINAL and len(p) > 2:
        if type(p[1]) != list:
            p[1] = []
        p[1].append(ValueExpr(p[2], p[3]))
        if isinstance(p[4], CommentExpr):
            p[1].append(p[4])
        p[0] = p[1]
    elif len(p) > 2:
        if isinstance(p[1], list):
            p[1].append(p[2])
            p[0] = p[1]
        else:
            p[0] = [p[2]]


def p_lc(p):
    """lc : BLOCK_COMMENT NEWLINE
    |"""
    if len(p) > 2:
        p[0] = p[1]


def p_o(p):
    """o : BLOCK_COMMENT o
    | NEWLINE o
    |"""
    breakpoint()
    if len(p) > 2 and _PRESERVE_ORIGINAL:
        if isinstance(p[2], CommentExpr):
            p[2].insert(0, p[1])
            p[0] = p[2]
        else:
            p[0] = CommentExpr([p[1]])


def p_error(p):
    """."""
    global parser
    next_token = parser.token()
    if next_token is None:
        use_token = 'EOF'
    else:
        use_token = repr(next_token.value)

    if p:
        raise ParseError(
            ' ERROR on "%s" (type %s, file %s, line %d, Next %s)'
            % (p.value, p.type, _CURRENT_PARSE_FILENAME, p.lineno, use_token)
        )
    else:
        raise ParseError(' ERROR you likely have unablanaced "{"\'s')


breakpoint()
parser = yacc.yacc(write_tables=False, debug=True, outputdir='.', errorlog=logging)


class CommentExprType(enum.Enum):
    CommentFile = enum.auto()
    CommentFilterInter = enum.auto()
    CommentBlockStart = enum.auto()
    CommentBlockInter = enum.auto()
    CommentFieldStart = enum.auto()
    CommentValueEOL = enum.auto()
    CommentValueInter = enum.auto()


class CommentExpr(list):
    """A sequence of comments and/or newlines."""

    # def __init__(self, value: "str | list[str]"):
    #     # self.expr_type = expr_type
    #     self.value = value


class ValueExpr:
    def __init__(self, value: "Any", comment: "Optional[str]" = None):
        self.value = value
        self.comment = comment


# pylint: enable=unused-argument,invalid-name,g-short-docstring-punctuation
# pylint: enable=g-docstring-quotes,g-short-docstring-space
# pylint: enable=g-space-before-docstring-summary,g-doc-args
# pylint: enable=g-no-space-after-docstring-summary
# pylint: enable=g-docstring-missing-newline


def _ReadFile(filename):
    """Read data from a file if it exists.

    Args:
      filename: str - Filename

    Returns:
      data: str contents of file.

    Raises:
      FileNotFoundError: if requested file does not exist.
      FileReadError: Any error resulting from trying to open/read file.
    """
    logging.debug('ReadFile(%s)', filename)
    if os.path.exists(filename):
        try:
            with open(filename, 'r') as f:
                data = f.read()
            return data
        except IOError:
            raise FileReadError('Unable to open or read file %s' % filename)
    else:
        raise FileNotFoundError('Unable to open policy file %s' % filename)


def _Preprocess(data, max_depth=5, base_dir=''):
    """Search input for include statements and import specified include file.

    Search input for include statements and if found, import specified file
    and recursively search included data for includes as well up to max_depth.

    Args:
      data: A string of Policy file data.
      max_depth: Maximum depth of included files
      base_dir: Base path string where to look for policy or include files

    Returns:
      A string containing result of the processed input data

    Raises:
      RecursionTooDeepError: nested include files exceed maximum
    """
    if not max_depth:
        raise RecursionTooDeepError(
            '%s' % ('Included files exceed maximum recursion depth of %s.' % max_depth)
        )
    rval = []
    for line in [x.rstrip() for x in data.splitlines()]:
        words = line.split()
        if len(words) > 1 and words[0] == '#include':
            # remove any quotes around included filename
            include_file = words[1].strip('\'"')
            include_path = os.path.join(base_dir, include_file)

            if pathlib.Path(include_path).suffix != '.inc':
                raise BadIncludePath(
                    f"Include file name must end in \".inc\". File={include_path} base_directory={base_dir}"
                )

            if not _SubpathOf(base_dir, include_path):
                raise BadIncludePath(
                    f"Include file cannot be loaded from outside the base directory. File={include_path} base_directory={base_dir}"
                )

            data = _ReadFile(include_path)
            # recursively handle includes in included data
            inc_data = _Preprocess(data, max_depth - 1, base_dir=base_dir)
            rval.extend(inc_data)
        else:
            rval.append(line)
    return rval


def _SubpathOf(parent, subpath):
    return str(pathlib.Path(subpath).resolve()).startswith(str(pathlib.Path(parent).resolve()))


def ParseFile(filename, definitions=None, optimize=True, base_dir='', shade_check=False):
    """Parse the policy contained in file, optionally provide a naming object.

    Read specified policy file and parse into a policy object.

    Args:
      filename: Name of policy file to parse.
      definitions: optional naming library definitions object.
      optimize: bool - whether to summarize networks and services.
      base_dir: base path string to look for acls or include files.
      shade_check: bool - whether to raise an exception when a term is shaded.

    Returns:
      policy object or False (if parse error).
    """
    data = _ReadFile(filename)
    p = ParsePolicy(
        data, definitions, optimize, base_dir=base_dir, shade_check=shade_check, filename=filename
    )
    return p


def ParsePolicy(
    data, definitions=None, optimize=True, base_dir='', shade_check=False, filename=''
):
    """Parse the policy in 'data', optionally provide a naming object.

    Parse a blob of policy text into a policy object.

    Args:
      data: a string blob of policy data to parse.
      definitions: optional naming library definitions object.
      optimize: bool - whether to summarize networks and services.
      base_dir: base path string to look for acls or include files.
      shade_check: bool - whether to raise an exception when a term is shaded.
      filename: string - filename used by the policy.

    Returns:
      policy object or False (if parse error).
    """
    try:
        if definitions:
            globals()['DEFINITIONS'] = definitions
        else:
            globals()['DEFINITIONS'] = naming.Naming(DEFAULT_DEFINITIONS)
        globals()['_OPTIMIZE'] = optimize
        globals()['_SHADE_CHECK'] = shade_check
        globals()['_PRESERVE_ORIGINAL'] = False
        globals()['_CURRENT_PARSE_FILENAME'] = filename

        lexer = lex.lex()

        preprocessed_data = '\n'.join(_Preprocess(data, base_dir=base_dir))
        global parser
        policy = parser.parse(preprocessed_data, lexer=lexer)
        policy.filename = filename
        return policy

    except IndexError:
        return False


def FromBuilder(builder: PolicyBuilder):
    """Construct and return a Policy model instance from a PolicyBuilder."""
    if builder.definitions:
        globals()['DEFINITIONS'] = builder.definitions
    else:
        globals()['DEFINITIONS'] = naming.Naming(DEFAULT_DEFINITIONS)
    globals()['_OPTIMIZE'] = builder.optimize
    globals()['_SHADE_CHECK'] = builder.shade_check
    globals()['_PRESERVE_ORIGINAL'] = False
    globals()['_CURRENT_PARSE_FILENAME'] = builder.raw_policy.filename

    return builder.BuildPolicy()


#
# Idea: two phase extraction
#
# Insight: .pol as a file format is relatively flat but has
# a very high number of unique value expressions. It also
# accepts newlines and block comments in very unusual places (in between
# "::", for example). The result is block comments must be present at all
# levels of the parse tree, which means all levels need to accomodate comments,
# and the code that processes the parse tree must perform many flattening operations
# in order to associate comments with objects and positions.
#
# Producing a full parse tree might be very complex and time consuming.
#
# Simplifying ideas:
#
# (1) It might be possible to have the parser print out a new file format
#     that is much simpler. A second parser could then consume the simplified
#     file format.
# (2) Attaching comments directly to VarType objects might help with early value association.
#     VarType objects are sometimes produced above several layers of grammar so
#     this simplifies up to a point.
#
# Example simplified language.
# The core idea is that comment classes are knowable fairly low in the parse tree, possibly
# at the grammar level. But hoisting comments up through several levels of grammar is complicated.
# Even though we can associate comments with values, VarType is not constructed until high
# in the parse tree, thus there are sub-values below VarType which can have comments associated. So
# VarType as it exists is not a great representation for comment-value associations. Sticking to
# traditional value/comment tuples is OK, and having some lower-level value type is OK (albeit
# converter-specific in both cases).
#
# So the alternative idea here is to "print as we go" during the parse, thus producing a
# fairly flat representation of the file as we parse. A second scan can then group these parts
# with clear association rules.
#
# It's also possible that "print" or "emit" are both valid options. In either case we are creating
# a side channel, so this actually might fit nicely into the original parser.
#
# Example: target service accounts. This is a basic list of names. Each one could potentially have
# a comment in between.
#
# Simplified:
#
# target-resources:: RESOURCE1 RESOURCE2
#
# With comments:
#
# target-resources #FieldStart
# #FieldStart
# : #FieldStart
# #FieldStart
# : #FieldStart
# #FieldStart
# RESOURCE1 #ValueEOL
# #ValueInter
# RESOURCE2 #ValueEOL
# #ValueInter
#
# Flatter:
# VarType 2  # TARGET_RESOURCES
# FieldStart
# comment 1
# comment 2
# comment 3
# comment 4
# comment 5
# Value RESOURCE1
# ValueEOL comment 1
# ValueInter
# comment 1
# Value RESOURCE2
# ValueEOL comment 1
#
# Ultimately we want to construct an object format something like this:
#
# VarType [
#   comment=['fieldstart comments']
#   value=list[
#     ValueExpr[
#       value='RESOURCE1'
#       eol_comment='line comment'
#     ],
#     CommentExpr['inter value comments'],
#     ValueExpr[
#       value='RESOURCE2'
#       eol_comment='line comment'
#     ]
#   ]
# ]
# CommentExpr['block inter comments (which may associate with the next VarType (field line))']
# VarType [
#   comment=['fieldstart comments']
#   value=list[
#     ValueExpr[
#       value='RESOURCE1'
#       eol_comment='line comment'
#     ],
#     CommentExpr['inter value comments'],
#     ValueExpr[
#       value='RESOURCE2'
#       eol_comment='line comment'
#     ]
#   ]
# ]
#
#
# And at the top level, something like this:
# PolicyParseData [
#   comment=['File comments']
#   data=list[
#     (
#     Header [
#        comment=['key line comments']
#        data=[...]
#     ],
#     list[
#       CommentExpr['inter termlist comments'],
#       TermParseData [
#           comment=['name line comments']
#           name='name'
#           data=[...]
#       ]
#     ],
#     CommentExpr['inter filter comments']
#     ),
#   ]
# ]


def ParseOriginal(data, definitions=None, filename='', is_include=False):
    """Partially parse 'data' into an PolicyParseData model, preserving the original
    content of 'data'. The PolicyParseData model produced can be passed to export.ExportPolicy().

    Compared to ParsePolicy, this function will ensure that includes are preserved,
    tokens are preserved and addresses and ports are not collapsed so a faithful copy can be taken.
    This also loosens some of the normal parse requirements: undefined tokens are allowed and include
    statements will not be loaded at all.

    ParseOriginal can also load include files.

    Args:
      data: a string blob of policy data to parse.
      filename: string - filename used by the policy.
      is_include: string - whether the file is an include file, a standalone term list that has no headers.

    Returns:
      A PolicyParseData object or False (if parse error).
    """
    if not definitions:
        definitions = naming.Naming(DEFAULT_DEFINITIONS)

    globals()['DEFINITIONS'] = definitions
    globals()['_OPTIMIZE'] = False
    globals()['_SHADE_CHECK'] = False
    globals()['_PRESERVE_ORIGINAL'] = True
    globals()['_CURRENT_PARSE_FILENAME'] = filename

    pol_copy = PolicyParseData(is_include=is_include)
    pol_copy.filename = filename
    data = pol_copy.Preprocess(data)

    try:
        lexer = lex.lex()

        global parser
        policy: PolicyParseData | list[str] | None = parser.parse(data, lexer=lexer)
        if isinstance(policy, PolicyParseData):
            pol_copy.data = policy.data
            pol_copy.block_comment = policy.block_comment
        elif isinstance(policy, list):
            pol_copy.block_comment = policy

        return pol_copy

    except IndexError as e:
        raise
        return False


class PolicyParseData:
    _include_counter = 1000

    def __init__(
        self,
        header: HeaderParseData = None,
        terms: TermParseData = None,
        comment: CommentExpr = None,
        *,
        is_include: bool = False,
    ):
        self.block_comment = []
        self.data = []
        self.filename = ''
        self.include_placeholders = []
        self.is_include = is_include

        if header or terms:
            self.AddFilter(header, terms, comment)

    def AddFilter(
        self, header: HeaderParseData, terms: TermParseData, comment: CommentExpr = None
    ):
        self.data.append((header, terms, comment))

    def AddBlockComment(self, comment):
        self.block_comment.append(comment)

    def Preprocess(self, data):
        if self.is_include:
            data = self._PreprocessWrapTermList(data)

        data = self._PreprocessIncludePlaceholders(data)

        return data

    def _PreprocessIncludePlaceholders(self, data):
        """Scan file and replace '#include' lines with placeholder terms.

        NOTE: this is not completely safe! #include lines can appear anywhere in the file, so
        it's possible some users are overly clever with them. Program should message user
        if that sort of scenario is detected."""

        rval = []
        for line in [x.rstrip() for x in data.splitlines()]:
            words = line.split()
            if len(words) > 1 and words[0] == '#include':
                include_file = words[1].strip('\'"')

                placeholder_term_name = f"PLACEHOLDER-INCLUDE-d5e4-f1c0-{self._include_counter}"
                self._include_counter = self._include_counter + 1

                placeholder_lines = f"""
term {placeholder_term_name} {{
  comment:: "{placeholder_term_name}"
  comment:: "{include_file}"
}}"""
                self.include_placeholders.append(placeholder_term_name)
                rval.append(placeholder_lines)
            else:
                rval.append(line)
        return '\n'.join(rval)

    @staticmethod
    def _PreprocessWrapTermList(data):
        """Encapsulate an include file in a dummy policy so we can load it as a Policy model.

        NOTE: this is not entirely safe! It assumes that an include file is a sequence of terms.
        However because .pol includes are inserted verbatim without attentino to structure, it's
        possible some users are overly clever with them. Program should try to message user
        if that sort of scenario is detected."""

        return f"""
header {{
  comment:: "PLACEHOLDER-TERMLIST-WRAPPER"
}}
{data}
"""


class CommentParseData:
    def __init__(self, data):
        self.data = data


# if you call this from the command line, you can specify a pol file for it to
# read.
if __name__ == '__main__':
    ret = 0
    if len(sys.argv) > 1:
        try:
            ret = ParsePolicy(open(sys.argv[1], 'r').read(), filename=sys.argv[1])
        except IOError:
            print('ERROR: \'%s\' either does not exist or is not readable' % (sys.argv[1]))
            ret = 1
    else:
        # default to reading stdin
        ret = ParsePolicy(sys.stdin.read())
    sys.exit(ret)
