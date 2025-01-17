# Copyright (c) 2016 Cisco Systems
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import logging  # noqa
import mock
import os

from oslo_config import cfg
from oslo_log import log as o_log
from oslo_utils import uuidutils
from oslotest import base
from sqlalchemy.orm import sessionmaker as sa_sessionmaker

from aim.agent.aid.universes.aci import aci_universe
from aim.agent.aid.universes.k8s import k8s_watcher
from aim import aim_manager
from aim import aim_store
from aim.api import resource
from aim.api import status as aim_status
from aim.common import utils
from aim import config as aim_cfg
from aim import context
from aim.db import api
from aim.db import hashtree_db_listener as ht_db_l
from aim.db import model_base
from aim.k8s import api_v1 as k8s_api_v1
from aim.tools.cli import shell  # noqa
from aim import tree_manager

CONF = cfg.CONF
ROOTDIR = os.path.dirname(__file__)
ETCDIR = os.path.join(ROOTDIR, 'etc')
o_log.register_options(aim_cfg.CONF)
K8S_STORE_VENV = 'K8S_STORE'
K8S_CONFIG_ENV = 'K8S_CONFIG'

LOG = o_log.getLogger(__name__)


def etcdir(*p):
    return os.path.join(ETCDIR, *p)


def resource_equal(self, other):

    if type(self) != type(other):
        return False
    for attr in self.identity_attributes:
        if getattr(self, attr) != getattr(other, attr):
            return False
    for attr in self.other_attributes:
        if (utils.deep_sort(getattr(self, attr, None)) !=
                utils.deep_sort(getattr(other, attr, None))):
            return False
    return True


def requires(requirements):
    def wrap(func):
        def inner(self, *args, **kwargs):
            diff = set(requirements) - set(self.ctx.store.features)
            if diff:
                self.skipTest("Store %s doesn't support required "
                              "features: %s" % (self.ctx.store.name, diff))
            else:
                func(self, *args, **kwargs)
        return inner
    return wrap


class BaseTestCase(base.BaseTestCase):
    """Test case base class for all unit tests."""

    def config_parse(self, conf=None, args=None):
        """Create the default configurations."""
        # neutron.conf.test includes rpc_backend which needs to be cleaned up
        if args is None:
            args = []
        args += ['--config-file', self.test_conf_file]
        if conf is None:
            CONF(args=args, project='aim')
        else:
            conf(args)
        o_log.setup(cfg.CONF, 'aim')

    def setUp(self):
        super(BaseTestCase, self).setUp()
        self.addCleanup(CONF.reset)
        self.test_conf_file = etcdir('aim.conf.test')
        self.config_parse()

    def _check_call_list(self, expected, mocked, check_all=True):
        observed = mocked.call_args_list
        for call in expected:
            self.assertTrue(call in observed,
                            msg='Call not found, expected:\n%s\nobserved:'
                                '\n%s' % (str(call), str(observed)))
            observed.remove(call)
        if check_all:
            self.assertFalse(
                len(observed),
                msg='There are more calls than expected: %s' % str(observed))


name_to_res = {utils.camel_to_snake(x.__name__): x for x in
               aim_manager.AimManager.aim_resources}
k8s_watcher_instance = None


def _k8s_post_create(self, created):
    if created:
        w = k8s_watcher_instance
        w.klient.get_new_watch()
        event = {'type': 'ADDED', 'object': created}
        w.klient.watch.stream = mock.Mock(return_value=[event])
        w._reset_trees = mock.Mock()
        w.q.put(event)
        w._persistence_loop(save_on_empty=True, warmup_wait=0)


def _k8s_post_delete(self, deleted):
    if deleted:
        w = k8s_watcher_instance
        event = {'type': 'DELETED', 'object': deleted}
        w.klient.get_new_watch()
        w.klient.watch.stream = mock.Mock(return_value=[event])
        w._reset_trees = mock.Mock()
        w.q.put(event)
        w._persistence_loop(save_on_empty=True, warmup_wait=0)


def _initialize_hooks(self):
    self.old_initialize_hooks()
    self.register_after_transaction_ends_callback('_catch_up_logs',
                                                  self._catch_up_logs)


def _catch_up_logs(self, added, updated, removed):
    # Create new session and populate the hashtrees
    session = api.get_session(autocommit=True, expire_on_commit=True,
                              use_slave=False)
    store = aim_store.SqlAlchemyStore(session)
    ht_db_l.HashTreeDbListener(
        aim_manager.AimManager()).catch_up_with_action_log(store)


class TestAimDBBase(BaseTestCase):

    _TABLES_ESTABLISHED = False

    def setUp(self, mock_store=True):
        super(TestAimDBBase, self).setUp()
        self.test_id = uuidutils.generate_uuid()
        aim_cfg.OPTION_SUBSCRIBER_MANAGER = None
        aci_universe.ws_context = None
        if not os.environ.get(K8S_STORE_VENV):
            CONF.set_override('aim_store', 'sql', 'aim')
            self.engine = api.get_engine()
            if not TestAimDBBase._TABLES_ESTABLISHED:
                model_base.Base.metadata.create_all(self.engine)
                TestAimDBBase._TABLES_ESTABLISHED = True

            # Uncomment the line below to log SQL statements. Additionally, to
            # log results of queries, change INFO to DEBUG
            #
            # logging.getLogger('sqlalchemy.engine').setLevel(logging.DEBUG)

            def clear_tables():
                with self.engine.begin() as conn:
                    for table in reversed(
                            model_base.Base.metadata.sorted_tables):
                        conn.execute(table.delete())
            self.addCleanup(clear_tables)
            if mock_store:
                self.old_initialize_hooks = (
                    aim_store.SqlAlchemyStore._initialize_hooks)
                aim_store.SqlAlchemyStore.old_initialize_hooks = (
                    self.old_initialize_hooks)
                aim_store.SqlAlchemyStore._initialize_hooks = _initialize_hooks

                def restore_initialize_hook():
                    aim_store.SqlAlchemyStore._initialize_hooks = (
                        self.old_initialize_hooks)
                self.addCleanup(restore_initialize_hook)
                aim_store.SqlAlchemyStore._catch_up_logs = _catch_up_logs
        else:
            CONF.set_override('aim_store', 'k8s', 'aim')
            CONF.set_override('k8s_namespace', self.test_id, 'aim_k8s')
            k8s_config_path = os.environ.get(K8S_CONFIG_ENV)
            if k8s_config_path:
                CONF.set_override('k8s_config_path', k8s_config_path,
                                  'aim_k8s')
            aim_store.K8sStore._post_delete = _k8s_post_delete
            aim_store.K8sStore._post_create = _k8s_post_create
            global k8s_watcher_instance
            k8s_watcher_instance = k8s_watcher.K8sWatcher()
            k8s_watcher_instance.event_handler = mock.Mock()
            k8s_watcher_instance._renew_klient_watch = mock.Mock()
            self.addCleanup(self._cleanup_objects)

        self.store = api.get_store(expire_on_commit=True)

        def unregister_catch_up():
            self.store.unregister_after_transaction_ends_callback(
                '_catch_up_logs')

        self.addCleanup(unregister_catch_up)
        self.ctx = context.AimContext(store=self.store)
        self.cfg_manager = aim_cfg.ConfigManager(self.ctx, '')
        self.tt_mgr = tree_manager.HashTreeManager()
        resource.ResourceBase.__eq__ = resource_equal
        self.cfg_manager.replace_all(CONF)
        self.sys_id = self.cfg_manager.get_option('aim_system_id', 'aim')

    def get_new_context(self):
        return context.AimContext(
            db_session=sa_sessionmaker(bind=self.engine)())

    def set_override(self, item, value, group=None, host='', poll=False):
        # Override DB config as well
        if group:
            CONF.set_override(item, value, group)
        else:
            CONF.set_override(item, value)
        self.cfg_manager.override(item, value, group=group or 'default',
                                  host=host, context=self.ctx)
        if poll:
            self.cfg_manager.subs_mgr._poll_and_execute()

    def _cleanup_objects(self):
        objs = [k8s_api_v1.Namespace(metadata={'name': self.test_id}),
                k8s_api_v1.Namespace(metadata={'name': 'ns-' + self.test_id}),
                k8s_api_v1.Node(metadata={'name': self.test_id})]
        for obj in objs:
            try:
                self.ctx.store.delete(obj)
            except k8s_api_v1.klient.ApiException as e:
                if str(e.status) != '420':
                    LOG.warning("Error while cleaning %s %s: %s",
                                obj.kind, obj['metadata']['name'], e)

    @classmethod
    def _get_example_aci_object(cls, type, dn, **kwargs):
        attr = {'dn': dn}
        attr.update(kwargs)
        return {type: {'attributes': attr}}

    @classmethod
    def _get_example_aim_security_group_rule(cls, **kwargs):
        example = resource.SecurityGroupRule(
            tenant_name='t1', security_group_name='sg1',
            security_group_subject_name='sgs1', name='rule1')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aim_bd(cls, **kwargs):
        example = resource.BridgeDomain(tenant_name='test-tenant',
                                        vrf_name='default',
                                        name='test', enable_arp_flood=False,
                                        enable_routing=True,
                                        limit_ip_learn_to_subnets=False,
                                        l2_unknown_unicast_mode='proxy',
                                        ep_move_detect_mode='')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_bd(cls, **kwargs):
        example_bd = {
            "fvBD": {
                "attributes": {
                    "arpFlood": "no", "descr": "test",
                    "dn": "uni/tn-test-tenant/BD-test",
                    "epMoveDetectMode": "",
                    "limitIpLearnToSubnets": "no",
                    "llAddr": "::",
                    "mac": "00:22:BD:F8:19:FF",
                    "multiDstPktAct": "bd-flood",
                    "name": "test", "displayName": "",
                    "ownerKey": "", "ownerTag": "", "unicastRoute": "yes",
                    "unkMacUcastAct": "proxy", "unkMcastAct": "flood",
                    "vmac": "not-applicable"}}}
        example_bd['fvBD']['attributes'].update(kwargs)
        return example_bd

    @classmethod
    def _get_example_aim_netflow(cls, **kwargs):
        example = resource.NetflowVMMExporterPol(name='netflow1',
                                                 dst_addr='172.28.184.76',
                                                 dst_port='2055',
                                                 ver='v9')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_netflow(cls, **kwargs):
        example_netflow = {
            "netflowVmmExporterPol": {
                "attributes": {
                    "dn": "uni/infra/vmmexporterpol-netflow1",
                    "name": "netflow1", "displayName": "",
                    "dstAddr": "172.28.184.76",
                    "dstPort": "2055",
                    "srcAddr": "0.0.0.0",
                    "ownerKey": "", "ownerTag": "",
                    "ver": "v9"}}}
        example_netflow['netflowVmmExporterPol']['attributes'].update(kwargs)
        return example_netflow

    @classmethod
    def _get_example_aim_vswitch_policy_grp(cls, **kwargs):
        example = resource.VmmVswitchPolicyGroup(domain_type='OpenStack',
                                                 domain_name='osd13-fab20')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_vswitch_policy_grp(cls, **kwargs):
        example_vs_pol_grp = {
            "vmmVSwitchPolicyCont": {
                "attributes": {
                    "dn": "uni/vmmp-OpenStack/dom-osd13-fab20/vswitchpolcont",
                    "ownerKey": "", "ownerTag": ""}}}
        example_vs_pol_grp['vmmVSwitchPolicyCont']['attributes'].update(kwargs)
        return example_vs_pol_grp

    @classmethod
    def _get_example_aim_reln_netflow(cls, **kwargs):
        example = resource.VmmRelationToExporterPol(domain_type='OpenStack',
                                                    domain_name='osd13-fab20',
                                                    netflow_path='uni/infra/'
                                                    'vmmexporterpol-test',
                                                    active_flow_time_out=90,
                                                    idle_flow_time_out=15,
                                                    sampling_rate=0)
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_reln_netflow(cls, **kwargs):
        example_reln = {
            "vmmRsVswitchExporterPol": {
                "attributes": {
                    "dn": "uni/vmmp-OpenStack/dom-osd13-fab20/vswitchpolcont/"
                          "rsvswitchExporterPol-[uni/infra/vmmexporterpol-"
                          "test]",
                    "activeFlowTimeOut": 90, "idleFlowTimeOut": 15,
                    "samplingRate": 0, "ownerKey": "", "ownerTag": ""}}}
        example_reln['vmmRsVswitchExporterPol']['attributes'].update(kwargs)
        return example_reln

    @classmethod
    def _get_example_aci_rs_ctx(cls, **kwargs):
        example_rsctx = {
            "fvRsCtx": {
                "attributes": {
                    "tnFvCtxName": "default",
                    "dn": "uni/tn-test-tenant/BD-test/rsctx"}}}
        example_rsctx['fvRsCtx']['attributes'].update(kwargs)
        return example_rsctx

    @classmethod
    def _get_example_aim_vrf(cls, **kwargs):
        example = resource.VRF(
            tenant_name='test-tenant',
            name='test',
            policy_enforcement_pref=resource.VRF.POLICY_ENFORCED)
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_vrf(cls, **kwargs):
        example_vrf = {
            "fvCtx": {
                "attributes": {
                    "dn": "uni/tn-test-tenant/ctx-test",
                    "descr": "",
                    "knwMcastAct": "permit",
                    "name": "default",
                    "ownerKey": "",
                    "ownerTag": "",
                    "pcEnfDir": "ingress",
                    "pcEnfPref": "enforced"
                }
            }
        }
        example_vrf['fvCtx']['attributes'].update(kwargs)
        return example_vrf

    @classmethod
    def _get_example_aim_app_profile(cls, **kwargs):
        example = resource.ApplicationProfile(
            tenant_name='test-tenant', name='test')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_app_profile(cls, **kwargs):
        example_ap = {
            "fvAp": {
                "attributes": {
                    "dn": "uni/tn-test-tenant/ap-test",
                    "descr": ""
                }
            }
        }
        example_ap['fvAp']['attributes'].update(kwargs)
        return example_ap

    @classmethod
    def _get_example_aim_subnet(cls, **kwargs):
        example = resource.Subnet(
            tenant_name='t1', bd_name='test', gw_ip_mask='10.10.10.0/28')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_subnet(cls, **kwargs):
        example_sub = {
            "fvSubnet": {
                "attributes": {
                    "dn": "uni/tn-t1/BD-test/subnet-[10.10.10.0/28]",
                    "scope": "private"
                }
            }
        }
        example_sub['fvSubnet']['attributes'].update(kwargs)
        return example_sub

    @classmethod
    def _get_example_aim_tenant(cls, **kwargs):
        example = resource.Tenant(name='test-tenant')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_tenant(cls, **kwargs):
        example_tenant = {
            "fvTenant": {
                "attributes": {
                    "dn": "uni/tn-test-tenant",
                    "descr": ""
                }
            }
        }
        example_tenant['fvTenant']['attributes'].update(kwargs)
        return example_tenant

    @classmethod
    def _get_example_aim_epg(cls, **kwargs):
        example = resource.EndpointGroup(
            tenant_name='t1', app_profile_name='a1', name='test',
            bd_name='net1')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_epg(cls, **kwargs):
        example_epg = {
            "fvAEPg": {
                "attributes": {
                    "dn": "uni/tn-t1/ap-a1/epg-test",
                    "descr": ""
                }
            }
        }
        example_epg['fvAEPg']['attributes'].update(kwargs)
        return example_epg

    @classmethod
    def _get_example_aim_fault(cls, **kwargs):
        example = aim_status.AciFault(
            fault_code='951',
            external_identifier='uni/tn-t1/ap-a1/epg-test/fault-951',
            severity='warning')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_fault(cls, **kwargs):
        example_epg = {
            "faultInst": {
                "attributes": {
                    "dn": "uni/tn-t1/ap-a1/epg-test/fault-951",
                    "descr": "cannot resolve",
                    "code": "951",
                    "severity": "warning",
                    "cause": "resolution-failed",
                }
            }
        }
        example_epg['faultInst']['attributes'].update(kwargs)
        return example_epg

    @classmethod
    def _get_example_aci_ext_net(cls, **kwargs):
        example_extnet = {
            "l3extInstP": {
                "attributes": {
                    "descr": "",
                    "dn": "uni/tn-common/out-default/instP-extnet",
                    "name": "extnet"}}}
        example_extnet['l3extInstP']['attributes'].update(kwargs)
        return example_extnet

    @classmethod
    def _get_example_aci_ext_net_rs_prov(cls, **kwargs):
        example_extnet = {
            "fvRsProv": {
                "attributes": {
                    "dn": "uni/tn-common/out-default/instP-extnet"
                          "/rsprov-default",
                    "tnVzBrCPName": "default"}}}
        example_extnet['fvRsProv']['attributes'].update(kwargs)
        return example_extnet

    @classmethod
    def _get_example_aci_l3_out(cls, **kwargs):
        example_l3o = {"l3extOut": {
            "attributes": {
                "descr": "",
                "dn": "uni/tn-common/out-default",
                "name": "default"}}}
        example_l3o['l3extOut']['attributes'].update(kwargs)
        return example_l3o

    @classmethod
    def _get_example_aci_l3_out_vrf_rs(cls, **kwargs):
        example_l3o_vrf_rs = {"l3extRsEctx": {
            "attributes": {
                "dn": "uni/tn-common/out-default/rsectx",
                "tnFvCtxName": "default"}}}
        example_l3o_vrf_rs['l3extRsEctx']['attributes'].update(kwargs)
        return example_l3o_vrf_rs

    @classmethod
    def _get_example_aci_contract(cls, **kwargs):
        example_brcp = {
            "vzBrCP": {
                "attributes": {
                    "dn": "uni/tn-common/brc-c",
                    "name": "c"}}}
        example_brcp['vzBrCP']['attributes'].update(kwargs)
        return example_brcp

    @classmethod
    def _get_example_aci_oob_contract(cls, **kwargs):
        example_oob_brcp = {
            "vzOOBBrCP": {
                "attributes": {
                    "dn": "uni/tn-common/oobbrc-c",
                    "name": "c"}}}
        example_oob_brcp['vzOOBBrCP']['attributes'].update(kwargs)
        return example_oob_brcp

    @classmethod
    def _get_example_provided_contract(cls, **kwargs):
        example_rsprov = {
            "fvRsProv": {
                "attributes": {
                    "dn": "uni/tn-common/ap-ap/epg-epg/rsprov-c",
                    "tnVzBrCPName": "c"
                }
            }
        }
        example_rsprov['fvRsProv']['attributes'].update(kwargs)
        return example_rsprov

    @classmethod
    def _get_example_consumed_contract(cls, **kwargs):
        example_rscons = {
            "fvRsCons": {
                "attributes": {
                    "dn": "uni/tn-common/ap-ap/epg-epg/rscons-c",
                    "tnVzBrCPName": "c"
                }
            }
        }
        example_rscons['fvRsCons']['attributes'].update(kwargs)
        return example_rscons

    @classmethod
    def generate_aim_object(cls, aim_type, **kwargs):
        """Generate AIM object with random identity attributes.

        Identity attributes will be considered as strings, which could be
        schema-invalid. kwargs can be passed to fix that.
        """
        res_dict = {x: utils.generate_uuid()
                    for x in aim_type.identity_attributes.keys()}
        res_dict.update(kwargs)
        return aim_type(**res_dict)

    @classmethod
    def _get_example_aim_span_vsource_grp(cls, **kwargs):
        example = resource.SpanVsourceGroup(name='testSrcGrp',
                                            admin_st='start')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_span_vsource_grp(cls, **kwargs):
        example_span_vsource_grp = {
            "spanVSrcGrp": {
                "attributes": {
                    "dn": "uni/infra/vsrcgrp-testSrcGrp",
                    "name": "testSrcGrp", "descr": "",
                    "adminSt": "start",
                    "ownerKey": "", "ownerTag": ""}}}
        example_span_vsource_grp['spanVSrcGrp']['attributes'].update(kwargs)
        return example_span_vsource_grp

    @classmethod
    def _get_example_aim_span_vsource(cls, **kwargs):
        example = resource.SpanVsource(vsg_name='testSrcGrp',
                                       name='testSrc',
                                       dir='both')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_span_vsource(cls, **kwargs):
        example_span_vsource = {
            "spanVSrc": {
                "attributes": {
                    "dn": "uni/infra/vsrcgrp-testSrcGrp/vsrc-testSrc",
                    "name": "testSrc", "descr": "", "dir": "both",
                    "ownerKey": "", "ownerTag": ""}}}
        example_span_vsource['spanVSrc']['attributes'].update(kwargs)
        return example_span_vsource

    @classmethod
    def _get_example_aim_span_vdest_grp(cls, **kwargs):
        example = resource.SpanVdestGroup(name='testDestGrp')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_span_vdest_grp(cls, **kwargs):
        example_span_vdest_grp = {
            "spanVDestGrp": {
                "attributes": {
                    "dn": "uni/infra/vdestgrp-testDestGrp",
                    "name": "testDestGrp", "descr": "",
                    "ownerKey": "", "ownerTag": ""}}}
        example_span_vdest_grp['spanVDestGrp']['attributes'].update(kwargs)
        return example_span_vdest_grp

    @classmethod
    def _get_example_aim_span_vdest(cls, **kwargs):
        example = resource.SpanVdest(vdg_name='testDestGrp',
                                     name='testDest')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_span_vdest(cls, **kwargs):
        example_span_vdest = {
            "spanVDest": {
                "attributes": {
                    "dn": "uni/infra/vdestgrp-testDestGrp/vdest-testDest",
                    "name": "testDest", "descr": "",
                    "ownerKey": "", "ownerTag": ""}}}
        example_span_vdest['spanVDest']['attributes'].update(kwargs)
        return example_span_vdest

    @classmethod
    def _get_example_aim_span_vepg_sum(cls, **kwargs):
        example = resource.SpanVepgSummary(vdg_name='testDestGrp',
                                           vd_name='testDest',
                                           dst_ip='172.51.12.2',
                                           flow_id=1,
                                           ttl=128,
                                           mtu=1519,
                                           mode='visible',
                                           src_ip_prefix='1.2.2.1',
                                           dscp=48)
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_span_vepg_sum(cls, **kwargs):
        example_span_vepg_sum = {
            "spanVEpgSummary": {
                "attributes": {
                    "dn": "uni/infra/vdestgrp-testDestGrp/vdest-testDest/"
                          "vepgsummary",
                    "dstIp": "172.51.12.2", "flowId": 1, "ttl": 128,
                    "mtu": 1519, "descr": "",
                    "mode": "visible", "srcIpPrefix": "1.1.1.1", "dscp": 32,
                    "ownerKey": "", "ownerTag": ""}}}
        example_span_vepg_sum['spanVEpgSummary']['attributes'].update(kwargs)
        return example_span_vepg_sum

    @classmethod
    def _get_example_aim_infra_acc_bundle_grp(cls, **kwargs):
        example = resource.InfraAccBundleGroup(name='accTest',
                                               lag_t='node',
                                               display_name='test_src')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_infra_acc_bundle_grp(cls, **kwargs):
        example_acc_bundle_grp = {
            "infraAccBndlGrp": {
                "attributes": {
                    "dn": "uni/infra/funcprof/accbundle-accTest",
                    "name": "accTest", "descr": "",
                    "ownerKey": "", "ownerTag": "", "lagT": "link"}}}
        example_acc_bundle_grp['infraAccBndlGrp']['attributes'].update(kwargs)
        return example_acc_bundle_grp

    @classmethod
    def _get_example_aim_infra_acc_port_grp(cls, **kwargs):
        example = resource.InfraAccPortGroup(name='1-5',
                                             display_name='test_dest')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_infra_acc_port_grp(cls, **kwargs):
        example_acc_port_grp = {
            "infraAccPortGrp": {
                "attributes": {
                    "dn": "uni/infra/funcprof/accportgrp-1-5",
                    "name": "1-5", "descr": "",
                    "ownerKey": "", "ownerTag": ""}}}
        example_acc_port_grp['infraAccPortGrp']['attributes'].update(kwargs)
        return example_acc_port_grp

    @classmethod
    def _get_example_aim_span_spanlbl(cls, **kwargs):
        example = resource.SpanSpanlbl(vsg_name='testSrcGrp',
                                       name='testDestGrp',
                                       tag='green-yellow')
        example.__dict__.update(kwargs)
        return example

    @classmethod
    def _get_example_aci_span_spanlbl(cls, **kwargs):
        example_span_spanlbl = {
            "spanSpanLbl": {
                "attributes": {
                    "dn": "uni/infra/vsrcgrp-testSrcGrp/spanlbl-testDestGrp",
                    "name": "testSrc", "descr": "", "ownerKey": "",
                    "ownerTag": "", "tag": ""}}}
        example_span_spanlbl['spanSpanLbl']['attributes'].update(kwargs)
        return example_span_spanlbl
