# Dataset gaps: what the read screen cannot measure

Generated from `screen_reads.py`, which enumerates every REST read endpoint from the URL
resolver. Of **166 endpoints, 66 return zero rows** and another **47 return fewer than ten**.

This matters for two separate reasons.

**An empty endpoint still measures its fixed cost**, so it is not wasted — but it cannot show
an N+1, because there are no rows to iterate. Every per-row defect this branch has found
(findings 30, 34, 36, 48, 53, 54, 56) was visible only because the table had rows in it.

**A thinly populated endpoint is worse than an empty one**, because it looks measured. A
three-row table renders three per-row queries, which reads as a small fixed cost rather than
a defect that scales. `api.interface.depth1` was a 1,229-query endpoint and it was found by
guessing that interfaces is the biggest table, not by the screen ranking it.

Order within each section is arbitrary; the app grouping is the useful axis for databot work.

## Zero rows (66 endpoints)

### dcim (22)

| model | endpoint | url |
|---|---|---|
| `consoleporttemplate` | `dcim-api:consoleporttemplate-list` | `/api/dcim/console-port-templates/?limit=25` |
| `consoleserverporttemplate` | `dcim-api:consoleserverporttemplate-list` | `/api/dcim/console-server-port-templates/?limit=25` |
| `devicebay` | `dcim-api:devicebay-list` | `/api/dcim/device-bays/?limit=25` |
| `devicebaytemplate` | `dcim-api:devicebaytemplate-list` | `/api/dcim/device-bay-templates/?limit=25` |
| `deviceclusterassignment` | `dcim-api:deviceclusterassignment-list` | `/api/dcim/device-cluster-assignments/?limit=25` |
| `devicetypetosoftwareimagefile` | `dcim-api:devicetypetosoftwareimagefile-list` | `/api/dcim/device-types-to-software-image-files/?limit=25` |
| `frontport` | `dcim-api:frontport-list` | `/api/dcim/front-ports/?limit=25` |
| `frontporttemplate` | `dcim-api:frontporttemplate-list` | `/api/dcim/front-port-templates/?limit=25` |
| `interfacetemplate` | `dcim-api:interfacetemplate-list` | `/api/dcim/interface-templates/?limit=25` |
| `interfacevdcassignment` | `dcim-api:interfacevdcassignment-list` | `/api/dcim/interface-vdc-assignments/?limit=25` |
| `module` | `dcim-api:module-list` | `/api/dcim/modules/?limit=25` |
| `modulebay` | `dcim-api:modulebay-list` | `/api/dcim/module-bays/?limit=25` |
| `modulebaytemplate` | `dcim-api:modulebaytemplate-list` | `/api/dcim/module-bay-templates/?limit=25` |
| `modulefamily` | `dcim-api:modulefamily-list` | `/api/dcim/module-families/?limit=25` |
| `moduletype` | `dcim-api:moduletype-list` | `/api/dcim/module-types/?limit=25` |
| `poweroutlettemplate` | `dcim-api:poweroutlettemplate-list` | `/api/dcim/power-outlet-templates/?limit=25` |
| `powerporttemplate` | `dcim-api:powerporttemplate-list` | `/api/dcim/power-port-templates/?limit=25` |
| `rackgroup` | `dcim-api:rackgroup-list` | `/api/dcim/rack-groups/?limit=25` |
| `rackreservation` | `dcim-api:rackreservation-list` | `/api/dcim/rack-reservations/?limit=25` |
| `rearport` | `dcim-api:rearport-list` | `/api/dcim/rear-ports/?limit=25` |
| `rearporttemplate` | `dcim-api:rearporttemplate-list` | `/api/dcim/rear-port-templates/?limit=25` |
| `virtualdevicecontext` | `dcim-api:virtualdevicecontext-list` | `/api/dcim/virtual-device-contexts/?limit=25` |

### extras (15)

| model | endpoint | url |
|---|---|---|
| `approvalworkflow` | `extras-api:approvalworkflow-list` | `/api/extras/approval-workflows/?limit=25` |
| `approvalworkflowstage` | `extras-api:approvalworkflowstage-list` | `/api/extras/approval-workflow-stages/?limit=25` |
| `configcontextschema` | `extras-api:configcontextschema-list` | `/api/extras/config-context-schemas/?limit=25` |
| `dynamicgroupmembership` | `extras-api:dynamicgroupmembership-list` | `/api/extras/dynamic-group-memberships/?limit=25` |
| `fileproxy` | `extras-api:fileproxy-list` | `/api/extras/file-proxies/?limit=25` |
| `imageattachment` | `extras-api:imageattachment-list` | `/api/extras/image-attachments/?limit=25` |
| `jobhook` | `extras-api:jobhook-list` | `/api/extras/job-hooks/?limit=25` |
| `metadatachoice` | `extras-api:metadatachoice-list` | `/api/extras/metadata-choices/?limit=25` |
| `note` | `extras-api:note-list` | `/api/extras/notes/?limit=25` |
| `savedview` | `extras-api:savedview-list` | `/api/extras/saved-views/?limit=25` |
| `scheduledjob` | `extras-api:scheduledjob-list` | `/api/extras/scheduled-jobs/?limit=25` |
| `staticgroupassociation` | `extras-api:staticgroupassociation-list` | `/api/extras/static-group-associations/?limit=25` |
| `tag` | `extras-api:tag-list` | `/api/extras/tags/?limit=25` |
| `usersavedviewassociation` | `extras-api:usersavedviewassociation-list` | `/api/extras/user-saved-view-associations/?limit=25` |
| `webhook` | `extras-api:webhook-list` | `/api/extras/webhooks/?limit=25` |

### load_balancers (7)

| model | endpoint | url |
|---|---|---|
| `certificateprofile` | `load_balancers-api:certificateprofile-list` | `/api/load-balancers/certificate-profiles/?limit=25` |
| `healthcheckmonitor` | `load_balancers-api:healthcheckmonitor-list` | `/api/load-balancers/health-check-monitors/?limit=25` |
| `loadbalancerpool` | `load_balancers-api:loadbalancerpool-list` | `/api/load-balancers/load-balancer-pools/?limit=25` |
| `loadbalancerpoolmember` | `load_balancers-api:loadbalancerpoolmember-list` | `/api/load-balancers/load-balancer-pool-members/?limit=25` |
| `loadbalancerpoolmembercertificateprofileassignment` | `load_balancers-api:loadbalancerpoolmembercertificateprofileassignment-list` | `/api/load-balancers/load-balancer-pool-member-certificate-profile-assignments/?limit=25` |
| `virtualserver` | `load_balancers-api:virtualserver-list` | `/api/load-balancers/virtual-servers/?limit=25` |
| `virtualservercertificateprofileassignment` | `load_balancers-api:virtualservercertificateprofileassignment-list` | `/api/load-balancers/virtual-server-certificate-profile-assignments/?limit=25` |

### cloud (6)

| model | endpoint | url |
|---|---|---|
| `cloudaccount` | `cloud-api:cloudaccount-list` | `/api/cloud/cloud-accounts/?limit=25` |
| `cloudnetwork` | `cloud-api:cloudnetwork-list` | `/api/cloud/cloud-networks/?limit=25` |
| `cloudnetworkprefixassignment` | `cloud-api:cloudnetworkprefixassignment-list` | `/api/cloud/cloud-network-prefix-assignments/?limit=25` |
| `cloudresourcetype` | `cloud-api:cloudresourcetype-list` | `/api/cloud/cloud-resource-types/?limit=25` |
| `cloudservice` | `cloud-api:cloudservice-list` | `/api/cloud/cloud-services/?limit=25` |
| `cloudservicenetworkassignment` | `cloud-api:cloudservicenetworkassignment-list` | `/api/cloud/cloud-service-network-assignments/?limit=25` |

### data_validation (5)

| model | endpoint | url |
|---|---|---|
| `datacompliance` | `data_validation-api:datacompliance-list` | `/api/data-validation/data-compliance/?limit=25` |
| `minmaxvalidationrule` | `data_validation-api:minmaxvalidationrule-list` | `/api/data-validation/min-max-rules/?limit=25` |
| `regularexpressionvalidationrule` | `data_validation-api:regularexpressionvalidationrule-list` | `/api/data-validation/regex-rules/?limit=25` |
| `requiredvalidationrule` | `data_validation-api:requiredvalidationrule-list` | `/api/data-validation/required-rules/?limit=25` |
| `uniquevalidationrule` | `data_validation-api:uniquevalidationrule-list` | `/api/data-validation/unique-rules/?limit=25` |

### virtualization (5)

| model | endpoint | url |
|---|---|---|
| `cluster` | `virtualization-api:cluster-list` | `/api/virtualization/clusters/?limit=25` |
| `clustergroup` | `virtualization-api:clustergroup-list` | `/api/virtualization/cluster-groups/?limit=25` |
| `clustertype` | `virtualization-api:clustertype-list` | `/api/virtualization/cluster-types/?limit=25` |
| `virtualmachine` | `virtualization-api:virtualmachine-list` | `/api/virtualization/virtual-machines/?limit=25` |
| `vminterface` | `virtualization-api:vminterface-list` | `/api/virtualization/interfaces/?limit=25` |

### ipam (4)

| model | endpoint | url |
|---|---|---|
| `ipaddressrange` | `ipam-api:ipaddressrange-list` | `/api/ipam/ip-address-ranges/?limit=25` |
| `rir` | `ipam-api:rir-list` | `/api/ipam/rirs/?limit=25` |
| `service` | `ipam-api:service-list` | `/api/ipam/services/?limit=25` |
| `vrfdeviceassignment` | `ipam-api:vrfdeviceassignment-list` | `/api/ipam/vrf-device-assignments/?limit=25` |

### circuits (1)

| model | endpoint | url |
|---|---|---|
| `providernetwork` | `circuits-api:providernetwork-list` | `/api/circuits/provider-networks/?limit=25` |

### users (1)

| model | endpoint | url |
|---|---|---|
| `token` | `users-api:token-list` | `/api/users/tokens/?limit=25` |

## Fewer than ten rows (47 endpoints)

Populated enough to measure, too thin for a per-row cost to be distinguishable.

| rows | model | app | endpoint |
|---:|---|---|---|
| 1 | `circuittype` | circuits | `circuits-api:circuittype-list` |
| 4 | `provider` | circuits | `circuits-api:provider-list` |
| 2 | `platform` | dcim | `dcim-api:platform-list` |
| 4 | `cabletype` | dcim | `dcim-api:cabletype-list` |
| 4 | `softwareimagefile` | dcim | `dcim-api:softwareimagefile-list` |
| 4 | `softwareversion` | dcim | `dcim-api:softwareversion-list` |
| 5 | `devicefamily` | dcim | `dcim-api:devicefamily-list` |
| 6 | `locationtype` | dcim | `dcim-api:locationtype-list` |
| 9 | `manufacturer` | dcim | `dcim-api:manufacturer-list` |
| 1 | `approvalworkflowdefinition` | extras | `extras-api:approvalworkflowdefinition-list` |
| 1 | `approvalworkflowstagedefinition` | extras | `extras-api:approvalworkflowstagedefinition-list` |
| 1 | `computedfield` | extras | `extras-api:computedfield-list` |
| 1 | `exporttemplate` | extras | `extras-api:exporttemplate-list` |
| 1 | `externalintegration` | extras | `extras-api:externalintegration-list` |
| 1 | `gitrepository` | extras | `extras-api:gitrepository-list` |
| 1 | `graphqlquery` | extras | `extras-api:graphqlquery-list` |
| 1 | `jobbutton` | extras | `extras-api:jobbutton-list` |
| 1 | `jobqueue` | extras | `extras-api:jobqueue-list` |
| 1 | `metadatatype` | extras | `extras-api:metadatatype-list` |
| 1 | `relationship` | extras | `extras-api:relationship-list` |
| 1 | `secretsgroup` | extras | `extras-api:secretsgroup-list` |
| 2 | `customfield` | extras | `extras-api:customfield-list` |
| 2 | `dynamicgroup` | extras | `extras-api:dynamicgroup-list` |
| 2 | `jobresult` | extras | `extras-api:jobresult-list` |
| 2 | `secret` | extras | `extras-api:secret-list` |
| 2 | `secretsgroupassociation` | extras | `extras-api:secretsgroupassociation-list` |
| 3 | `customlink` | extras | `extras-api:customlink-list` |
| 3 | `team` | extras | `extras-api:team-list` |
| 4 | `customfieldchoice` | extras | `extras-api:customfieldchoice-list` |
| 6 | `contact` | extras | `extras-api:contact-list` |
| 6 | `joblogentry` | extras | `extras-api:joblogentry-list` |
| 1 | `namespace` | ipam | `ipam-api:namespace-list` |
| 4 | `routetarget` | ipam | `ipam-api:routetarget-list` |
| 4 | `vrf` | ipam | `ipam-api:vrf-list` |
| 1 | `tenantgroup` | tenancy | `tenancy-api:tenantgroup-list` |
| 5 | `tenant` | tenancy | `tenancy-api:tenant-list` |
| 2 | `user` | users | `users-api:user-list` |
| 3 | `group` | users | `users-api:group-list` |
| 3 | `objectpermission` | users | `users-api:objectpermission-list` |
| 1 | `vpn` | vpn | `vpn-api:vpn-list` |
| 4 | `vpnprofilephase1policyassignment` | vpn | `vpn-api:vpnprofilephase1policyassignment-list` |
| 4 | `vpnprofilephase2policyassignment` | vpn | `vpn-api:vpnprofilephase2policyassignment-list` |
| 5 | `vpnphase1policy` | vpn | `vpn-api:vpnphase1policy-list` |
| 5 | `vpnphase2policy` | vpn | `vpn-api:vpnphase2policy-list` |
| 5 | `vpnprofile` | vpn | `vpn-api:vpnprofile-list` |
| 3 | `wirelessnetwork` | wireless | `wireless-api:wirelessnetwork-list` |
| 7 | `supporteddatarate` | wireless | `wireless-api:supporteddatarate-list` |

## Already known to be empty from the model side

`VirtualMachine`, `Cluster` and `VirtualDeviceContext` are at zero rows and `JobResult` has
two. These block three queue candidates outright: finding 53's property-column blind spot
(`VirtualMachineUIViewSet` and `VirtualDeviceContextTable` both declare the `primary_ip`
property column) and finding 54's two remaining per-cell-compiling columns
(`DeviceComponentNameColumn`, `JobResultColumn`). None can be priced until the generator
creates the objects.

