"""
Copyright 2018, Oath Inc.
Licensed under the terms of the Apache 2.0 license. See LICENSE file in project root for terms.
"""
from builtins import zip
import collections
import re

from cached_property import threaded_cached_property

from yahoo_panoptes.enrichment.schema.generic import snmp
from yahoo_panoptes.framework import const, enrichment
from yahoo_panoptes.plugins.enrichment.generic.snmp import plugin_enrichment_generic_snmp

from yahoo_panoptes.framework.utilities.snmp.mibs.snmpv2 import MibSNMPV2
from yahoo_panoptes.framework.utilities.snmp.mibs.juniper import MibJuniper

# n.b. For QFX1000X devices, will report % fan_trays_ok, which is <= to fans_ok
FAN_TYPES = [r'Fan Tray \d+ Fan \d+', r'Fan Tray \d+', r'FAN \d+', r'node\d SRX\d+ \w+ fan \d', r'node\d Fan \d',
             r'node\d \w+ Tray Fan \d+', r'(Top|Bottom)\s(Rear|Middle|Front)\sFan']
POWER_MODULE_TYPES = [r'PDM \d{1,2}$', 'PEM', r'PSM \d{1,2}$', r'Power Supply \d$', r'Power Supply: Power Supply \d+ @',
                      r'node\d PEM \d']
TYPE_MAP = dict(list(zip(POWER_MODULE_TYPES, [u'PDM', u'PEM', u'PSM', u'PEM', u'PEM', u'PEM'])))


class JuniperDeviceMetricsEnrichment(snmp.PanoptesGenericSNMPMetricsEnrichmentGroup):
    pass


class JuniperPluginEnrichmentDeviceMetrics(plugin_enrichment_generic_snmp.PanoptesEnrichmentGenericSNMPPlugin):
    def __init__(self):
        self._juniper_model = None
        super(JuniperPluginEnrichmentDeviceMetrics, self).__init__()

    def _get_cpu_interval(self):
        """
        Checks the self._execute_frequency to figure out the oid to use for cpu_utilization
        Returns:
            string: the oid to use.
        """
        self._polling_execute_frequency = int(self._plugin_conf['main']['polling_frequency'])

        if 5 <= self._polling_execute_frequency < 300:
            # TODO Need to divide by number of cores?
            # https://kb.juniper.net/InfoCenter/index?page=content&id=KB31764&cat=MX960_1&actp=LIST
            return str(MibJuniper.jnxOperating1MinAvgCPU)
        elif 300 <= self._polling_execute_frequency < 900:
            return str(MibJuniper.jnxOperating5MinAvgCPU)
        elif 900 <= self._polling_execute_frequency:
            return str(MibJuniper.jnxOperating15MinAvgCPU)
        else:
            return str(MibJuniper.jnxOperating1MinAvgCPU)

    @threaded_cached_property
    def _entity_names(self):
        entities = {}
        varbinds = self._snmp_connection.bulk_walk(MibJuniper.jnxOperatingDescr.oid)
        for varbind in varbinds:
            value = varbind.value
            if isinstance(value, bytes):
                value = value.decode(u'ascii', u'ignore')  # pragma: no cover
            entities[varbind.index] = value

        return entities

    @threaded_cached_property
    def _temp_sensors(self):
        """
        Returns:
             dict: temperature stats for the system
        """
        temps = {}
        varbinds = self._snmp_connection.bulk_walk(str(MibJuniper.jnxOperatingTemp))
        for varbind in varbinds:
            if 0 < int(varbind.value) < const.MELTING_POINT_STEEL:
                temp_id = varbind.index
                temps[temp_id] = {u'sensor_name': self._entity_names[temp_id]}
        return temps

    @threaded_cached_property
    def _cpus(self):
        """
        cpu will always be a Gauge32
        Returns:
            dict: cpus in the system
        """
        cpus = {}
        varbinds = self._snmp_connection.bulk_walk(self._get_cpu_interval())
        for varbind in varbinds:
            # grab the last element of the index to use as the cpu_id
            cpu_id = varbind.index  # TODO trim off prepending OID?

            for type in ['routing engine', 'fpc', 'fpm', 'cp', 'pic', 'fbc']:
                if type in self._entity_names[cpu_id].lower():
                    cpus[cpu_id] = {u'cpu_name': self._entity_names[cpu_id],
                                    u'cpu_no': u'Module ' + str(cpu_id)}
                    break

        return cpus

    @threaded_cached_property
    def _memory(self):
        """
        Returns:
             dict: memory stats for the system
        """
        memory = {}
        varbinds = self._snmp_connection.bulk_walk(str(MibJuniper.jnxOperatingMemory))
        for varbind in varbinds:
            memory_id = varbind.index
            memory[memory_id] = {u'memory_total': int(varbind.value) * (2 ** 20)}  # reported in megabytes
        return memory

    @threaded_cached_property
    def _fans(self):
        """
        Reports fan status for Junipers.
        Returns:
            dict: fan metrics
        """
        fans = {}
        for index, name in list(self._entity_names.items()):
            for type in FAN_TYPES:
                if re.match(type, name):
                    fans[index] = {u'name': name}

        return fans

    @threaded_cached_property
    def _power_modules(self):
        """
        Reports power entry module stats for Juniper devices.
        Returns:
            dict: power_supplies
        """
        power_modules = {}
        for index, name in list(self._entity_names.items()):
            for type in POWER_MODULE_TYPES:
                if re.match(type, name):
                    power_modules[index] = {u'name': name}
                    power_modules[index][u'type'] = TYPE_MAP[type]

        return power_modules

    @threaded_cached_property
    def _storage_descriptions(self):
        """
        Reports hrStorageDescriptions for Juniper devices.
        Returns:
            dict: storage_description

        {
            "snmpIndex.X": "/dev/gpt/junos: root file system, mounted on: /.mount",
            ...
        }
        """
        storage_description = {}
        varbinds = self._snmp_connection.bulk_walk(str(MibSNMPV2.hrStorageDescr))
        for varbind in varbinds:
            storage_description[varbind.index] = self.decode_bytes(varbind.value)
        return storage_description

    @threaded_cached_property
    def _storage_type(self):
        """
        Returns hrStorageType for Juniper devices.
            ( https://tools.ietf.org/html/rfc2790.html ) - PG 66
            hrStorageOther - 1
            hrStorageRam - 2
            hrStorageVirtualMemory - 3
            hrStorageFixedDisk - 4
            hrStorageRemovableDisk - 5
            hrStorageFloppyDisk - 6
            hrStorageCompactDisc - 7
            hrStorageRamDisk - 8
            hrStorageFlashMemory - 9
            hrStorageNetworkDisk - 10

        Returns:
            dict: storage_type
        :return:

            "1": ".1.3.6.1.2.1.25.2.1.9",
            "5": ".1.3.6.1.2.1.25.2.1.4",
            "6": ".1.3.6.1.2.1.25.2.1.4",
            "7": ".1.3.6.1.2.1.25.2.1.4",
            "8": ".1.3.6.1.2.1.25.2.1.4",
        """

        map_var_to_storage = {
            '1': 'hrStorageOther',
            '2': 'hrStorageRam',
            '3': 'hrStorageVirtualMemory',
            '4': 'hrStorageFixedDisk',
            '5': 'hrStorageRemovableDisk',
            '6': 'hrStorageFloppyDisk',
            '7': 'hrStorageCompactDisc',
            '8': 'hrStorageRamDisk',
            '9': 'hrStorageFlashMemory',
            '10': 'hrStorageNetworkDisk'
        }

        storage_type = {}
        varbinds = self._snmp_connection.bulk_walk(str(MibSNMPV2.hrStorageType))
        for varbind in varbinds:
            storage_type[varbind.index] = map_var_to_storage.get(self.decode_bytes(varbind.value).split(".")[-1], 'unknown')
        return storage_type

    @threaded_cached_property
    def _storage_allocation_units(self):
        storage_block_size = {}
        varbinds = self._snmp_connection.bulk_walk(str(MibSNMPV2.hrStorageAllocationUnits))
        for varbind in varbinds:
            storage_block_size[varbind.index] = int(varbind.value)
        return storage_block_size

    @threaded_cached_property
    def _storage_size(self):
        storage_size = {}
        varbinds = self._snmp_connection.bulk_walk(str(MibSNMPV2.hrStorageSize))

        allocation_units = self._storage_allocation_units

        for varbind in varbinds:
            if varbind.index in list(allocation_units.keys()):
                storage_size[varbind.index] = int(varbind.value) * allocation_units[varbind.index]
        return storage_size

    def _add_power_module_types_mapping(self):
        types_mapping = {x: x for x in list(self._oids_map[u"power_module_types"][u"values"].values())}
        self._oids_map[u"power_module_types"][u"values"].update(types_mapping)

    def _build_oids_map(self):
        self._oids_map = {
            u"cpu_name": {
                u"method": u"static",
                u"values": {x: self._cpus[x][u'cpu_name'] for x in self._cpus}
            },
            u"cpu_no": {
                u"method": u"static",
                u"values": {x: self._cpus[x][u'cpu_no'] for x in self._cpus}
            },
            u"cpu_util": {
                u"method": u"bulk_walk",
                u"oid": MibJuniper.jnxOperatingCPU.oid if 'EX4300' in self._juniper_model else
                    self._get_cpu_interval()
            },
            u"memory_used": {
                u"method": u"bulk_walk",
                u"oid": str(MibJuniper.jnxOperatingBuffer)
            },
            u"memory_total": {
                u"method": u"static",
                u"values": {x: self._memory[x][u'memory_total'] for x in self._memory if
                            self._memory[x][u'memory_total'] != 0}
            },
            u"oper_status": {
                u"method": u"bulk_walk",
                u"oid": str(MibJuniper.jnxOperatingState)
            },
            u"fans": {
                u"method": u"static",
                u"values": {x: self._fans[x][u'name'] for x in self._fans}
            },
            u"power_modules": {
                u"method": u"static",
                u"values": {x: self._power_modules[x][u'name'] for x in self._power_modules}
            },
            u"power_module_types": {
                u"method": u"static",
                u"values": {x: self._power_modules[x][u'type'] for x in self._power_modules}
            },
            u"power_units_total": {
                u"method": u"static",
                u"values": dict(collections.Counter([self._power_modules[x][u'type'] for x in self._power_modules]))
            },
            u"temp_sensor_values": {
                u"method": u"bulk_walk",
                u"oid": str(MibJuniper.jnxOperatingTemp)
            },
            u"temp_sensor_name": {
                u"method": u"static",
                u"values": {x: self._temp_sensors[x][u'sensor_name'] for x in self._temp_sensors}
            },
        }

        if self._plugin_conf.get('metrics_group', {}).get('include_disk_metrics_group', 0):

            oids_map = {
                u"storage_description": {
                    u"method": u"static",
                    u"values": self._storage_descriptions
                },
                u"storage_type": {
                    u"method": u"static",
                    u"values": self._storage_type
                },
                u"storage_allocation_failures": {
                    u"method": u"bulk_walk",
                    u"oid": str(MibSNMPV2.hrStorageAllocationFailures)
                },
                u"storage_allocation_units": {
                    u"method": u"static",
                    u"values": self._storage_allocation_units
                },
                u"storage_used_bytes": {
                    u"method": u"bulk_walk",
                    u"oid": str(MibSNMPV2.hrStorageUsed)
                },
                u"storage_total_bytes": {
                    u"method": u"static",
                    u"values": self._storage_size
                }
            }

            self._oids_map.update(oids_map)

        self._add_power_module_types_mapping()

    def _build_metrics_groups_conf(self):
        self._metrics_groups = [
            {
                u"group_name": u"environment",
                u"dimensions": {
                    u"sensor": u"temp_sensor_name.$index"
                },
                u"metrics": {
                    u"temperature_fahrenheit": {
                        u"metric_type": u"gauge",
                        u"type": u"float",
                        u"transform": u"lambda x: round((x * 1.8) + 32, 2) if x != 0 else 0.0",
                        u"value": u"temp_sensor_values.$index"
                    }
                }
            },
            {
                u"group_name": u"cpu",
                u"dimensions": {
                    u"cpu_name": u"cpu_name.$index",
                    u"cpu_no": u"cpu_no.$index",
                    u"cpu_type": u"'data' if 'Routing Engine' in cpu_name.$index else 'ctrl'"
                },
                u"metrics": {
                    u"cpu_utilization": {
                        u"metric_type": u"gauge",
                        u"value": u"cpu_util.$index"
                    }
                }
            },
            {
                u"group_name": u"memory",
                u"dimensions": {
                    u"memory_type": u"cpu_name.$index"
                },
                u"metrics": {
                    u"memory_used": {
                        u"metric_type": u"gauge",
                        u"indices_from": u"memory_total",
                        u"value": u"float(memory_used.$index) / 100.0 * memory_total.$index"
                    },
                    u"memory_total": {
                        u"metric_type": u"gauge",
                        u"value": u"memory_total.$index"
                    }
                }
            },
        ]

        if len(self._power_modules) > 0:
            self._metrics_groups.append(
                {
                    u"group_name": u"environment",
                    u"dimensions": {
                        u"power_module_type": u"power_module_types.$index"
                    },
                    u"metrics": {
                        u"power_units_on": {
                            u"metric_type": u"gauge",
                            u"indices_from": u"power_units_total",
                            u"value": u"len([(x,y) for (x,y) in oper_status.items() if x in "
                                      u"power_module_types and y not in "
                                      u"['6'] and power_module_types[x] == $index])"
                        },
                        u"power_units_total": {
                            u"metric_type": u"gauge",
                            u"value": u"power_units_total.$index"
                        }
                    }
                }
            )

        if len(self._fans) > 0:
            self._metrics_groups.append(
                {
                    u"group_name": u"environment",
                    u"dimensions": {},
                    u"metrics": {
                        u"fans_ok": {
                            u"metric_type": u"gauge",
                            u"value": u"len([(x,y) for (x,y) in oper_status.items() if x in fans and y not in ['6']])"
                        },
                        u"fans_total": len(self._fans)
                    }
                }
            )

        if len(list(self._storage_descriptions.keys())) > 0 and \
                self._plugin_conf.get('metrics_group', {}).get('include_disk_metrics_group', 0):
            self._metrics_groups.append(
                {
                    u"group_name": u"disk",
                    u"dimensions": {
                        u"storage_description": u"storage_description.$index",
                        u"storage_type": u"storage_type.$index"
                    },
                    u"metrics": {
                        u"storage_allocation_failures": {
                            u"metric_type": u"counter",
                            u"value": u"storage_allocation_failures.$index",
                        },
                        u"storage_used_bytes": {
                            u"metric_type": u"gauge",
                            u"value": u"int(storage_used_bytes.$index) * int(storage_allocation_units.$index)"
                        },
                        u"storage_total_bytes": {
                            u"metric_type": u"gauge",
                            u"value": u"storage_total_bytes.$index"
                        }
                    }
                },
            )

    @property
    def metrics_enrichment_class(self):
        return JuniperDeviceMetricsEnrichment

    def get_enrichment(self):
        self._juniper_model = self._plugin_context.data.resource_metadata.get(u'model', u'unknown')

        self._build_oids_map()
        self._build_metrics_groups_conf()

        enrichment_set = {
            u"oids": self.oids_map,
            u"metrics_groups": self.metrics_groups
        }

        try:
            self.enrichment_group.add_enrichment_set(enrichment.PanoptesEnrichmentSet(self.device_fqdn, enrichment_set))
        except Exception as e:
            self._logger.error(u'Error while adding enrichment set {} to enrichment group for the device {}: {}'.
                               format(enrichment_set, self.device_fqdn, repr(e)))

        self.enrichment_group_set.add_enrichment_group(self.enrichment_group)

        self._logger.debug(u'Metrics enrichment for device {}: {}'.format(self.device_fqdn, self.enrichment_group_set))

        return self.enrichment_group_set
