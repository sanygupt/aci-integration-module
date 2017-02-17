# Copyright (c) 2016 Cisco Systems
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import datetime

from oslo_config import cfg
from oslo_log import log as logging
from sqlalchemy.sql.expression import func

from aim.api import types as t
from aim.common import utils
from aim import exceptions as exc

# TODO(amitbose) Move ManagedObjectClass definitions to AIM
from apicapi import apic_client


LOG = logging.getLogger(__name__)


class ResourceBase(object):
    """Base class for AIM resource.

    Class property 'identity_attributes' gives a list of resource
    attributes that uniquely identify the resource. The values of
    these attributes directly determines the corresponding ACI
    object identifier (DN). These attributes must always be specified.
    Class property 'other_attributes' gives a list of additional
    resource attributes that are defined on the resource.
    Class property 'db_attributes' gives a list of resource attributes
    that are managed by the database layer, eg: timestamp, incremental counter.
    """

    db_attributes = t.db()

    def __init__(self, defaults, **kwargs):
        unset_attr = [k for k in self.identity_attributes
                      if kwargs.get(k) is None and k not in defaults]
        if 'display_name' in self.other_attributes:
            defaults.setdefault('display_name', '')
        if unset_attr:
            raise exc.IdentityAttributesMissing(attr=unset_attr)
        if kwargs.pop('_set_default', True):
            for k, v in defaults.iteritems():
                setattr(self, k, v)
        for k, v in kwargs.iteritems():
            setattr(self, k, v)

    @property
    def identity(self):
        return [getattr(self, x) for x in self.identity_attributes.keys()]

    @classmethod
    def attributes(cls):
        return (cls.identity_attributes.keys() + cls.other_attributes.keys() +
                cls.db_attributes.keys())

    @property
    def members(self):
        return {x: self.__dict__[x] for x in self.attributes() +
                ['pre_existing'] + ['_error'] if x in self.__dict__}

    def __str__(self):
        return '%s(%s)' % (type(self).__name__, ','.join(self.identity))

    def __eq__(self, other):
        return self.__dict__ == other.__dict__

    def __repr__(self):
        return '%s(%s)' % (super(ResourceBase, self).__repr__(), self.members)


class AciResourceBase(ResourceBase):
    """Base class for AIM resources that map to ACI objects.

    Child classes must define the following class attributes:
    * _tree_parent: Type of parent class in ACI tree structure
    * _aci_mo_name: ManagedObjectClass name of corresponding ACI object
    """
    tenant_ref_attribute = 'tenant_name'

    UNSPECIFIED = t.UNSPECIFIED

    def __init__(self, defaults, **kwargs):
        cls = type(self)
        for ra in ['_tree_parent', '_aci_mo_name']:
            if not hasattr(cls, ra):
                raise exc.AciResourceDefinitionError(attr=ra, klass=cls)
        super(AciResourceBase, self).__init__(defaults, **kwargs)

    @property
    def dn(self):
        return apic_client.ManagedObjectClass(self._aci_mo_name).dn(
            *self.identity)

    @classmethod
    def from_dn(cls, dn):
        DNMgr = apic_client.DNManager
        try:
            rns = DNMgr().aci_decompose(dn, cls._aci_mo_name)
            if len(rns) < len(cls.identity_attributes):
                raise exc.InvalidDNForAciResource(dn=dn, cls=cls)
            attr = {p[0]: p[1] for p in zip(cls.identity_attributes, rns)}
            return cls(**attr)
        except DNMgr.InvalidNameFormat:
            raise exc.InvalidDNForAciResource(dn=dn, cls=cls)


class Tenant(AciResourceBase):
    """Resource representing a Tenant in ACI.

    Identity attribute is RN for ACI tenant.
    """
    tenant_ref_attribute = 'name'

    identity_attributes = t.identity(
        ('name', t.name))
    other_attributes = t.other(
        ('display_name', t.name),
        ('monitored', t.bool))

    _aci_mo_name = 'fvTenant'
    _tree_parent = None

    def __init__(self, **kwargs):
        super(Tenant, self).__init__({'monitored': False}, **kwargs)


class BridgeDomain(AciResourceBase):
    """Resource representing a BridgeDomain in ACI.

    Identity attributes are RNs for ACI tenant and bridge-domain.
    """

    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('name', t.name))
    other_attributes = t.other(
        ('display_name', t.name),
        ('vrf_name', t.name),
        ('enable_arp_flood', t.bool),
        ('enable_routing', t.bool),
        ('limit_ip_learn_to_subnets', t.bool),
        ('l2_unknown_unicast_mode', t.enum("", "flood", "proxy")),
        ('ep_move_detect_mode', t.enum("", "garp")),
        ('l3out_names', t.list_of_names),
        ('monitored', t.bool))

    _aci_mo_name = 'fvBD'
    _tree_parent = Tenant

    def __init__(self, **kwargs):
        super(BridgeDomain, self).__init__({'vrf_name': '',
                                            'enable_arp_flood': True,
                                            'enable_routing': True,
                                            'limit_ip_learn_to_subnets': False,
                                            'l2_unknown_unicast_mode': 'proxy',
                                            'ep_move_detect_mode': 'garp',
                                            'l3out_names': [],
                                            'monitored': False},
                                           **kwargs)


class Agent(ResourceBase):
    """Resource representing an AIM Agent"""

    identity_attributes = t.identity(('id', t.id))
    other_attributes = t.other(
        ('agent_type', t.string(255)),
        ('host', t.string(255)),
        ('binary_file', t.string(255)),
        ('admin_state_up', t.bool),
        ('description', t.string(255)),
        ('hash_trees', t.list_of_ids),
        ('beat_count', t.number),
        ('version', t.string()))
    # Attrbutes completely managed by the DB (eg. timestamps)
    db_attributes = t.db(('heartbeat_timestamp', t.string()))

    def __init__(self, **kwargs):
        super(Agent, self).__init__({'admin_state_up': True,
                                     'beat_count': 0,
                                     'id': utils.generate_uuid()}, **kwargs)

    def __eq__(self, other):
        return self.id == other.id

    def is_down(self, context):
        current = context.db_session.query(func.now()).scalar()
        result = current - self.heartbeat_timestamp >= datetime.timedelta(
            seconds=cfg.CONF.aim.agent_down_time)
        if result:
            LOG.warn("Agent %s is down. Last heartbeat was %s" %
                     (self.id, self.heartbeat_timestamp))
        else:
            LOG.debug("Agent %s is alive, its last heartbeat was %s" %
                      (self.id, self.heartbeat_timestamp))
        return result

    def down_time(self, context):
        if self.is_down(context):
            current = context.db_session.query(func.now()).scalar()
            return (current - self.heartbeat_timestamp).seconds


class Subnet(AciResourceBase):
    """Resource representing a Subnet in ACI.

    Identity attributes: name of ACI tenant, name of bridge-domain and
    IP-address & mask of the default gateway in CIDR format (that is
    <gateway-address>/<prefix-len>). Helper function 'to_gw_ip_mask'
    may be used to construct the IP-address & mask value.
    """

    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('bd_name', t.name),
        ('gw_ip_mask', t.ip_cidr))
    other_attributes = t.other(
        ('scope', t.enum("", "public", "private", "shared")),
        ('display_name', t.name),
        ('monitored', t.bool))

    _aci_mo_name = 'fvSubnet'
    _tree_parent = BridgeDomain

    SCOPE_PRIVATE = 'private'
    SCOPE_PUBLIC = 'public'

    def __init__(self, **kwargs):
        super(Subnet, self).__init__({'scope': self.SCOPE_PUBLIC,
                                      'monitored': False}, **kwargs)

    @staticmethod
    def to_gw_ip_mask(gateway_ip_address, prefix_len):
        return '%s/%d' % (gateway_ip_address, prefix_len)


class VRF(AciResourceBase):
    """Resource representing a VRF (Layer3 network context) in ACI.

    Identity attributes: name of ACI tenant, name of VRF.
    """

    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('name', t.name))
    other_attributes = t.other(
        ('display_name', t.name),
        ('policy_enforcement_pref', t.enum("", "enforced", "unenforced")),
        ('monitored', t.bool))

    _aci_mo_name = 'fvCtx'
    _tree_parent = Tenant

    POLICY_ENFORCED = 'enforced'
    POLICY_UNENFORCED = 'unenforced'

    def __init__(self, **kwargs):
        super(VRF, self).__init__(
            {'policy_enforcement_pref': self.POLICY_ENFORCED,
             'monitored': False},
            **kwargs)


class ApplicationProfile(AciResourceBase):
    """Resource representing an application-profile in ACI.

    Identity attributes: name of ACI tenant, name of app-profile.
    """

    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('name', t.name))
    other_attributes = t.other(
        ('display_name', t.name),
        ('monitored', t.bool))

    _aci_mo_name = 'fvAp'
    _tree_parent = Tenant

    def __init__(self, **kwargs):
        super(ApplicationProfile, self).__init__({'monitored': False},
                                                 **kwargs)


class EndpointGroup(AciResourceBase):
    """Resource representing an endpoint-group in ACI.

    Identity attributes: name of ACI tenant, name of application-profile
    and name of endpoint-group.

    Attribute 'static_paths' is a list of dicts with the following keys:
    * path: (Required) path-name of the switch-port which is bound to
            EndpointGroup
    * encap: (Required) encapsulation mode and identifier for
            this EndpointGroup on the specified switch-port. Must be specified
            in the format 'vlan-<vlan-id>' for VLAN encapsulation
    """
    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('app_profile_name', t.name),
        ('name', t.name))
    other_attributes = t.other(
        ('display_name', t.name),
        ('bd_name', t.name),
        ('policy_enforcement_pref', t.enum("", "enforced", "unenforced")),
        ('provided_contract_names', t.list_of_names),
        ('consumed_contract_names', t.list_of_names),
        ('openstack_vmm_domain_names', t.list_of_names),
        ('physical_domain_names', t.list_of_names),
        ('static_paths', t.list_of_static_paths),
        ('monitored', t.bool))

    _aci_mo_name = 'fvAEPg'
    _tree_parent = ApplicationProfile

    POLICY_UNENFORCED = 'unenforced'
    POLICY_ENFORCED = 'enforced'

    def __init__(self, **kwargs):
        super(EndpointGroup, self).__init__({'bd_name': '',
                                             'provided_contract_names': [],
                                             'consumed_contract_names': [],
                                             'openstack_vmm_domain_names': [],
                                             'physical_domain_names': [],
                                             'policy_enforcement_pref':
                                             self.POLICY_UNENFORCED,
                                             'static_paths': [],
                                             'monitored': False},
                                            **kwargs)


class Filter(AciResourceBase):
    """Resource representing a contract filter in ACI.

    Identity attributes: name of ACI tenant and name of filter.
    """

    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('name', t.name))
    other_attributes = t.other(
        ('display_name', t.name),
        ('monitored', t.bool))

    _aci_mo_name = 'vzFilter'
    _tree_parent = Tenant

    def __init__(self, **kwargs):
        super(Filter, self).__init__({'monitored': False}, **kwargs)


class FilterEntry(AciResourceBase):
    """Resource representing a classifier entry of a filter in ACI.

    Identity attributes: name of ACI tenant, name of filter and name of entry.

    Values for classification fields may be integers as per standards
    (e.g. ip_protocol = 6 for TCP, 17 for UDP), or special strings listed
    below. UNSPECIFIED may be used to indicate that a particular
    field should be ignored.

    Field             | Special string values
    --------------------------------------------------------------------------
    arp_opcode        | req, reply
    ether_type        | trill, arp, mpls_ucast, mac_security, fcoe, ip
    ip_protocol       | icmp, igmp, tcp, egp, igp, udp, icmpv6, eigrp, ospfigp
    icmpv4_type       | echo-rep, dst-unreach, src-quench, echo, time-exceeded
    icmpv6_type       | dst-unreach, time-exceeded, echo-req, echo-rep,
                      | nbr-solicit, nbr-advert, redirect
    source_from_port, | ftpData, smtp, dns, http, pop3, https, rtsp
    source_to_port,   |
    dest_from_port,   |
    dest_to_port      |
    tcp_flags         | est, syn, ack, fin, rst

    """

    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('filter_name', t.name),
        ('name', t.name))
    other_attributes = t.other(
        ('display_name', t.name),
        ('arp_opcode', t.string()),
        ('ether_type', t.string()),
        ('ip_protocol', t.string()),
        ('icmpv4_type', t.string()),
        ('icmpv6_type', t.string()),
        ('source_from_port', t.port),
        ('source_to_port', t.port),
        ('dest_from_port', t.port),
        ('dest_to_port', t.port),
        ('tcp_flags', t.string()),
        ('stateful', t.bool),
        ('fragment_only', t.bool),
        ('monitored', t.bool))

    _aci_mo_name = 'vzEntry'
    _tree_parent = Filter

    def __init__(self, **kwargs):
        super(FilterEntry, self).__init__(
            {'arp_opcode': self.UNSPECIFIED,
             'ether_type': self.UNSPECIFIED,
             'ip_protocol': self.UNSPECIFIED,
             'icmpv4_type': self.UNSPECIFIED,
             'icmpv6_type': self.UNSPECIFIED,
             'source_from_port': self.UNSPECIFIED,
             'source_to_port': self.UNSPECIFIED,
             'dest_from_port': self.UNSPECIFIED,
             'dest_to_port': self.UNSPECIFIED,
             'tcp_flags': self.UNSPECIFIED,
             'stateful': False,
             'fragment_only': False,
             'monitored': False},
            **kwargs)


class Contract(AciResourceBase):
    """Resource representing a contract in ACI.

    Identity attributes: name of ACI tenant and name of contract.
    """

    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('name', t.name))
    other_attributes = t.other(
        ('scope', t.enum("", "tenant", "context", "global",
                         "application-profile")),
        ('display_name', t.name),
        ('monitored', t.bool))

    _aci_mo_name = 'vzBrCP'
    _tree_parent = Tenant

    SCOPE_APP_PROFILE = 'application-profile'
    SCOPE_TENANT = 'tenant'
    SCOPE_CONTEXT = 'context'
    SCOPE_GLOBAL = 'global'

    def __init__(self, **kwargs):
        super(Contract, self).__init__({'scope': self.SCOPE_CONTEXT,
                                        'monitored': False}, **kwargs)


class ContractSubject(AciResourceBase):
    """Resource representing a subject within a contract in ACI.

    Identity attributes: name of ACI tenant, name of contract and
    name of subject.
    """

    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('contract_name', t.name),
        ('name', t.name))
    other_attributes = t.other(
        ('display_name', t.name),
        ('in_filters', t.list_of_names),
        ('out_filters', t.list_of_names),
        ('bi_filters', t.list_of_names),
        ('monitored', t.bool))

    _aci_mo_name = 'vzSubj'
    _tree_parent = Contract

    def __init__(self, **kwargs):
        super(ContractSubject, self).__init__(
            {'in_filters': [], 'out_filters': [], 'bi_filters': [],
             'monitored': False}, **kwargs)


class Endpoint(ResourceBase):
    """Resource representing an endpoint.

    Identity attribute: UUID of the endpoint.
    """

    identity_attributes = t.identity(
        ('uuid', t.id))
    other_attributes = t.other(
        ('display_name', t.name),
        ('epg_tenant_name', t.name),
        ('epg_app_profile_name', t.name),
        ('epg_name', t.name))

    def __init__(self, **kwargs):
        super(Endpoint, self).__init__({'epg_name': None,
                                        'epg_tenant_name': None,
                                        'epg_app_profile_name': None},
                                       **kwargs)


class VMMDomain(ResourceBase):
    """Resource representing a VMM domain.

    Identity attributes: VMM type (eg. Openstack) and name
    """

    identity_attributes = t.identity(
        ('type', t.enum("VMWare", "OpenStack")),
        ('name', t.name))
    # REVISIT(ivar): A VMM has a plethora of attributes, references and child
    # objects that needs to be created. For now, this will however be just
    # the stub connecting what is explicitly created through the Infra and
    # what is managed by AIM, therefore we keep the stored information to
    # the very minimum
    other_attributes = t.other()
    _aci_mo_name = 'vmmDomP'
    _tree_parent = None

    def __init__(self, **kwargs):
        super(VMMDomain, self).__init__({}, **kwargs)


class PhysicalDomain(ResourceBase):
    """Resource representing a Physical domain.

    Identity attributes: name
    """

    identity_attributes = t.identity(('name', t.name))
    # REVISIT(ivar): A Physical Domain has a plethora of attributes, references
    # and child objects that needs to be created. For now, this will however be
    # just the stub connecting what is explicitly created through the Infra and
    # what is managed by AIM, therefore we keep the stored information to
    # the very minimum
    other_attributes = t.other()
    _aci_mo_name = 'physDomP'
    _tree_parent = None

    def __init__(self, **kwargs):
        super(PhysicalDomain, self).__init__({}, **kwargs)


class L3Outside(AciResourceBase):
    """Resource representing an L3 Outside.

    Identity attributes: name of ACI tenant, name of L3Out.
    """

    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('name', t.name))
    other_attributes = t.other(
        ('display_name', t.name),
        ('vrf_name', t.name),
        ('l3_domain_dn', t.string()),
        ('monitored', t.bool))

    _aci_mo_name = 'l3extOut'
    _tree_parent = Tenant

    def __init__(self, **kwargs):
        super(L3Outside, self).__init__(
            {'vrf_name': '', 'l3_domain_dn': '',
             'monitored': False}, **kwargs)


class ExternalNetwork(AciResourceBase):
    """Resource representing an external network instance profile.

    External network is a group of external subnets that have the same
    security behavior.

    Identity attributes: name of ACI tenant, name of L3Out, name of external
    network.
    """

    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('l3out_name', t.name),
        ('name', t.name))
    other_attributes = t.other(
        ('display_name', t.name),
        ('nat_epg_dn', t.string()),
        ('provided_contract_names', t.list_of_names),
        ('consumed_contract_names', t.list_of_names),
        ('monitored', t.bool))

    _aci_mo_name = 'l3extInstP'
    _tree_parent = L3Outside

    def __init__(self, **kwargs):
        super(ExternalNetwork, self).__init__(
            {'nat_epg_dn': '',
             'provided_contract_names': [], 'consumed_contract_names': [],
             'monitored': False},
            **kwargs)


class ExternalSubnet(AciResourceBase):
    """Resource representing an external subnet.

    Identity attributes: name of ACI tenant, name of L3Out, name of external
    network, network CIDR of the subnet.
    """

    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('l3out_name', t.name),
        ('external_network_name', t.name),
        ('cidr', t.ip_cidr))
    other_attributes = t.other(
        ('display_name', t.name),
        ('monitored', t.bool))

    _aci_mo_name = 'l3extSubnet'
    _tree_parent = ExternalNetwork

    def __init__(self, **kwargs):
        super(ExternalSubnet, self).__init__({'monitored': False}, **kwargs)


class SecurityGroup(AciResourceBase):
    """Resource representing a Security Group in ACI.

    Identity attributes: name of ACI tenant and name of security group
    """

    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('name', t.name))
    other_attributes = t.other(
        ('display_name', t.name),
        ('monitored', t.bool))

    _aci_mo_name = 'hostprotPol'
    _tree_parent = Tenant

    def __init__(self, **kwargs):
        super(SecurityGroup, self).__init__({'monitored': False}, **kwargs)


class SecurityGroupSubject(AciResourceBase):
    """Resource representing a subject within a security group in ACI.

    Identity attributes: name of ACI tenant, name of security group and
    name of subject.
    """

    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('security_group_name', t.name),
        ('name', t.name))
    other_attributes = t.other(
        ('display_name', t.name),
        ('monitored', t.bool))

    _aci_mo_name = 'hostprotSubj'
    _tree_parent = SecurityGroup

    def __init__(self, **kwargs):
        super(SecurityGroupSubject, self).__init__({'monitored': False},
                                                   **kwargs)


class SecurityGroupRule(AciResourceBase):
    """Resource representing a SG subject's rule in ACI.

    Identity attributes: name of ACI tenant, name of security group, name of
    subject and name of rule
    """
    identity_attributes = t.identity(
        ('tenant_name', t.name),
        ('security_group_name', t.name),
        ('security_group_subject_name', t.name),
        ('name', t.name))
    other_attributes = t.other(
        ('display_name', t.name),
        ('direction', t.enum("", "ingress", "egress")),
        ('ethertype', t.enum("", "undefined", "ipv4", "ipv6")),
        ('remote_ips', t.list_of_strings),
        ('ip_protocol', t.string()),
        ('from_port', t.port),
        ('to_port', t.port),
        ('monitored', t.bool))

    _aci_mo_name = 'hostprotRule'
    _tree_parent = SecurityGroupSubject

    def __init__(self, **kwargs):
        super(SecurityGroupRule, self).__init__(
            {'direction': 'ingress',
             'ethertype': "undefined",
             'remote_ips': [],
             'ip_protocol': self.UNSPECIFIED,
             'from_port': self.UNSPECIFIED,
             'to_port': self.UNSPECIFIED,
             'monitored': False}, **kwargs)
