# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import copy
import signal
import sys
import zun.conf

CONF = zun.conf.CONF

image_driver_list = [driver for driver in CONF.image_driver_list]

image_driver_list_with_none = image_driver_list + [None, 'None']

non_negative_integer = {
    'type': ['integer', 'string'],
    'pattern': '^[0-9]*$', 'minimum': 0
}

positive_integer = {
    'type': ['integer', 'string'],
    'pattern': '^[0-9]*$', 'minimum': 1
}

boolean_extended = {
    'type': ['boolean', 'string'],
    'enum': [True, 'True', 'TRUE', 'true', '1', 'ON', 'On', 'on',
             'YES', 'Yes', 'yes',
             False, 'False', 'FALSE', 'false', '0', 'OFF', 'Off', 'off',
             'NO', 'No', 'no'],
}

boolean = {
    'type': ['boolean', 'string'],
    'enum': [True, 'True', 'true', False, 'False', 'false'],
}

image_driver = {
    'type': ['string', 'null'],
    'enum': image_driver_list_with_none
}

container_name = {
    'type': ['string', 'null'],
    # A single character is a name: kubernetes accepts container names of one
    # character, and a capsule built from such a pod was rejected outright.
    # Both the length and the pattern had to change -- the old pattern
    # required a second character of its own accord.
    'minLength': 1,
    'maxLength': 255,
    'pattern': '^[a-zA-Z0-9][a-zA-Z0-9_.-]*$'
}

hex_uuid = {
    'type': 'string',
    'maxLength': 32,
    'minLength': 32,
    'pattern': '^[a-fA-F0-9]*$'
}

image_name = {
    'type': 'string',
    'minLength': 2,
    'maxLength': 255,
    'pattern': '[a-zA-Z0-9][a-zA-Z0-9_.-]'
}

image_host = {
    'type': 'string',
    'minLength': 2,
    'maxLength': 255,
    'pattern': '[a-zA-Z0-9][a-zA-Z0-9_.-]'
}

command = {
    'type': ['string', 'null']
}

command_list = {
    'type': ['array', 'null']
}

entrypoint = {
    'type': ['array', 'null']
}

auto_remove = {
    'type': ['boolean', 'null']
}

cpu = {
    'type': ['number', 'string', 'null'],
    'pattern': r'^[0-9]*(\.([0-9]+))?$',
    'minLength': 1,
    'minimum': CONF.minimum_cpus,
    'maximum': CONF.maximum_cpus,
}

cpu_policy = {
    'type': 'string',
    'enum': ['dedicated', 'shared']
}

# TODO(pksingh) Memory provided must be in MBs
# Will find another way if people dont find it useful.
memory = {
    'type': ['string', 'integer', 'null'],
    'minimum': CONF.minimum_memory,
    'maximum': CONF.maximum_memory,
    'pattern': '^[0-9]+$'
}

disk = {
    'type': ['string', 'integer', 'null'],
    'minimum': CONF.minimum_disk,
    'maximum': CONF.maximum_disk,
    'pattern': '^[0-9]+$'
}

workdir = {
    'type': ['string', 'null']
}

image_pull_policy = {
    'type': ['string', 'null'],
    'enum': ['never', 'always', 'ifnotpresent',
             'Never', 'Always', 'IfNotPresent', None]
}

labels = {
    'type': ['object', 'null']
}

annotations = {
    'type': ['object', 'null']
}

hints = {
    'type': ['object', 'null']
}

nets = {
    'type': ['array', 'null'],
    'items': {
        'type': 'object',
        'properties': {
            'network': {
                'type': ['string'],
                'minLength': 1,
                'maxLength': 255,
            },
            'v4-fixed-ip': {
                'type': ['string'],
                'format': 'ipv4'
            },
            'v6-fixed-ip': {
                'type': ['string'],
                'format': 'ipv6'
            },
            'port': {
                'type': ['string'],
                'maxLength': 255,
                'minLength': 1,
            }
        },
        'additionalProperties': False,
        'oneOf': [
            {
                'required': ['network']
            },
            {
                'required': ['port']
            }
        ]
    }
}

availability_zone = {
    'type': ['string', 'null'],
    'minLength': 1,
    'maxLength': 255,
}

healthcheck = {
    'type': ['object', 'null'],
    'items': {
        'type': 'object',
        'properties': {
            'cmd': {
                'type': ['string'],
                'minLength': 1,
                'maxLength': 255
            },
            'interval': {
                'type': ['integer', 'null']
            },
            'retries': {
                'type': ['integer', 'null']
            },
            'timeout': {
                'type': ['integer', 'null']
            }
        },
        'additionalProperties': False
    }
}

exposed_ports = {
    'type': ['object', 'null']
}

mounts = {
    'type': ['array', 'null'],
    'items': {
        'type': 'object',
        'properties': {
            'type': {
                'type': ['string'],
            },
            'source': {
                'type': ['string'],
            },
            'destination': {
                'type': ['string'],
            },
            'size': {
                'type': ['string', 'integer'],
            }
        },
        'additionalProperties': False,
        'anyOf': [
            {
                'required': ['source', 'destination']
            },
            {
                'required': ['size', 'destination']
            }
        ]
    }
}

environment = {
    'type': ['object', 'null'],
    'patternProperties': {
        '.+': {
            'type': ['string']
        },
    },
}

hostname = {
    'type': ['string', 'null'],
    'minLength': 2,
    'maxLength': 63
}

runtime = {
    'type': ['string', 'null'],
}

image_id = {
    'type': ['string', 'null'],
    'minLength': 2,
    'maxLength': 255,
    'pattern': '[a-zA-Z0-9][a-zA-Z0-9_.-]'
}

repo = {
    'type': 'string',
    'minLength': 2,
    'maxLength': 255,
    'pattern': '[a-zA-Z0-9][a-zA-Z0-9_.-]'
}


tag = copy.deepcopy(image_id)

size = {
    'type': ['string', 'integer', 'null'],
    'pattern': '^[0-9]+[b|B|k|K|m|M|g|G]?$',
}

restart_policy = {
    'type': ['object', 'null'],
    "properties": {
        "Name": {"type": ["string"],
                 "enum": ['no', 'always', 'on-failure', 'unless-stopped']},
        "MaximumRetryCount": {"type": ['integer', 'string', 'null'],
                              "minimum": 0, 'pattern': '^[0-9]+$'},
    },
    "additionalProperties": False,
    "required": ['Name']
}

string_ps_args = {
    'type': ['string'],
    'pattern': '[a-zA-Z- ,+]*'
}

str_and_int = {
    'type': ['string', 'integer', 'null'],
}

logs_since = {
    'type': ['string', 'integer', 'null'],
    'pattern': '(^[0-9]*$)|\
([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{1,3})'
}

exec_id = {
    'type': 'string',
    'maxLength': 64,
    'minLength': 64,
    'pattern': '^[a-f0-9]*$'
}

hostname = {
    'type': 'string', 'minLength': 1, 'maxLength': 255,
    # NOTE: 'host' is defined in "services" table, and that
    # means a hostname. The hostname grammar in RFC952 does
    # not allow for underscores in hostnames. However, this
    # schema allows them, because it sometimes occurs in
    # real systems.
    'pattern': '^[a-zA-Z0-9-._]*$',
}

SIGNALS = ['None']
if sys.version_info >= (3, 5, 0):
    signals = [n.name for n in signal.Signals]
    for s in signals:
        SIGNALS.append(s)
        SIGNALS.append(s.replace('SIG', ''))
        SIGNALS.append(s.lower())
        SIGNALS.append(s.lower().replace('sig', ''))
        SIGNALS.append(str(int(getattr(signal, s))))
else:
    signals = [n for n in dir(signal) if n.startswith('SIG') and '_' not in n]
    for s in signals:
        SIGNALS.append(s)
        SIGNALS.append(s.replace('SIG', ''))
        SIGNALS.append(s.lower())
        SIGNALS.append(s.lower().replace('sig', ''))
        SIGNALS.append(str(getattr(signal, s)))

signal = {
    'type': ['string', 'null'],
    'enum': SIGNALS
}

exec_command = {
    'type': ['string'],
    'minLength': 1,
}

security_groups = {
    'type': ['array', 'null'],
    'items': {
        'type': 'string',
        'minLength': 1,
        'maxLength': 255
    }
}

capsule_kind = {
    "type": ["string"],
    'enum': ['capsule', 'Capsule']
}

capsule_version = {
    "type": ["string"],
    'enum': ['beta', 'Beta']
}

capsule_metadata = {
    "type": ["object"],
    "properties": {
        "labels": labels,
        "annotations": annotations,
        # use the same format as container name
        "name": container_name,
    }
}

capsule_restart_policy = {
    "type": ["string"],
    "enum": ['Always', 'OnFailure', 'Never']
}

capsule_container_command = {
    'type': ['array'],
    'items': command
}

capsule_container_args = capsule_container_command

capsule_container_resources = {
    'type': ['object'],
    'properties': {
        'requests': {
            "type": ["object"],
            'properties': {
                'cpu': cpu,
                'memory': memory,
            },
            'additionalProperties': False,
        },
    },
    "additionalProperties": False,
}

capsule_port_protocol = {
    "type": ["string"],
    'enum': ['TCP', 'UDP']
}

capsule_container_ports = {
    'type': ['array'],
    'items': {
        'type': 'object',
        'properties': {
            'name': container_name,
            'containerPort': non_negative_integer,
            'hostPort': non_negative_integer,
            'protocol': capsule_port_protocol,
        },
        'additionalProperties': False,
        'required': ['containerPort']
    }
}

volume_name = {
    'type': ['string'],
    'minLength': 2,
    'maxLength': 255,
    'pattern': '^[a-zA-Z0-9][a-zA-Z0-9_.-]+$'
}

capsule_volume_path = {
    'type': ['string']
}

capsule_container_volume_list = {
    'type': ['array'],
    'items': {
        'type': 'object',
        'properties': {
            'name': volume_name,
            'mountPath': capsule_volume_path,
            'readOnly': boolean,
        },
        'additionalProperties': False,
        'required': ['name', 'mountPath']
    }
}


# A Kubernetes probe as it arrives in a capsule template. The shape follows
# core/v1.Probe so a caller can pass the pod's probe through unchanged; what a
# capsule can actually execute is decided by the driver, not here.
capsule_probe = {
    'type': ['object', 'null'],
    'properties': {
        'exec': {
            'type': 'object',
            'properties': {
                'command': {'type': 'array', 'items': {'type': 'string'}},
            },
            'additionalProperties': False,
        },
        'httpGet': {
            'type': 'object',
            'properties': {
                'path': {'type': ['string', 'null']},
                'port': {'type': ['integer', 'string']},
                'host': {'type': ['string', 'null']},
                'scheme': {'type': ['string', 'null'],
                           'enum': ['HTTP', 'HTTPS', None]},
                'httpHeaders': {'type': ['array', 'null']},
            },
            'additionalProperties': False,
        },
        'tcpSocket': {
            'type': 'object',
            'properties': {
                'port': {'type': ['integer', 'string']},
                'host': {'type': ['string', 'null']},
            },
            'additionalProperties': False,
        },
        'grpc': {
            'type': 'object',
            'properties': {
                'port': {'type': ['integer']},
                'service': {'type': ['string', 'null']},
            },
            'additionalProperties': False,
        },
        'initialDelaySeconds': {'type': ['integer', 'null']},
        'periodSeconds': {'type': ['integer', 'null']},
        'timeoutSeconds': {'type': ['integer', 'null']},
        'successThreshold': {'type': ['integer', 'null']},
        'failureThreshold': {'type': ['integer', 'null']},
        'terminationGracePeriodSeconds': {'type': ['integer', 'null']},
    },
    'additionalProperties': False,
}

# What of a Kubernetes securityContext the CRI runtime can be told. Anything
# outside this is refused rather than dropped: the field that goes missing is
# always the one that made the container safer.
capsule_security_context = {
    'type': ['object'],
    'properties': {
        'runAsUser': non_negative_integer,
        'runAsGroup': non_negative_integer,
        'readOnlyRootFilesystem': boolean,
        'allowPrivilegeEscalation': boolean,
        'capabilities': {
            'type': ['object'],
            'properties': {
                'add': {'type': ['array'], 'items': {'type': 'string'}},
                'drop': {'type': ['array'], 'items': {'type': 'string'}},
            },
            'additionalProperties': False,
        },
        'seccompProfile': {
            'type': ['object'],
            'properties': {
                'type': {'type': ['string'],
                         'enum': ['RuntimeDefault', 'Unconfined', 'Localhost']},
                'localhostProfile': {'type': ['string']},
            },
            'additionalProperties': False,
        },
    },
    'additionalProperties': False,
}

capsule_containers_list = {
    'type': ['array'],
    'items': {
        'type': 'object',
        'properties': {
            'name': container_name,
            'image': image_name,
            'command': capsule_container_command,
            'args': capsule_container_args,
            'resources': capsule_container_resources,
            'ports': capsule_container_ports,
            'volumeMounts': capsule_container_volume_list,
            'env': environment,
            'workDir': workdir,
            'imagePullPolicy': image_pull_policy,
            'stdin': boolean,
            'tty': boolean,
            'livenessProbe': capsule_probe,
            'readinessProbe': capsule_probe,
            'startupProbe': capsule_probe,
            'securityContext': capsule_security_context,
        },
        'additionalProperties': False,
        'required': ['image']
    }
}

volume_size = {
    'type': ['number'],
    'pattern': '^[0-9]*$',
    'minLength': 1
}

volume_auto_remove = {
    'type': boolean,
}

volume_uuid = {
    'type': 'string',
    'maxLength': 36,
    'minLength': 36
}

capsule_cinder_volume = {
    'type': 'object',
    'properties': {
        'volumeID': volume_uuid,
        'size': volume_size,
        'autoRemove': boolean,
    },
    'additionalProperties': False,
}

# A file whose content travels with the capsule, for configuration a container
# reads from disk rather than from its environment. The content is base64 so it
# survives binary data and newlines intact.
capsule_file_volume = {
    'type': 'object',
    'properties': {
        'contents': {
            'type': 'string',
            'maxLength': 1048576,
        },
    },
    'additionalProperties': False,
    'required': ['contents'],
}

# A directory that lives and dies with the capsule, shared by every container
# that mounts it. The pod-level scratch space nearly every workload assumes:
# a sidecar writing where another reads, a web server's cache directory, a
# socket two processes meet on.
capsule_empty_dir_volume = {
    'type': 'object',
    'properties': {
        # "Memory" makes it a tmpfs, which is what a caller asking for one
        # wants: speed, and content that never reaches a disk. Anything else
        # is a directory on the node.
        'medium': {'type': ['string', 'null'], 'enum': ['', 'Memory', None]},
        # Bytes. Only meaningful for a tmpfs, where it is enforced by the
        # kernel; on a node directory nothing here can enforce it, and
        # pretending otherwise would be worse than saying so.
        'sizeLimit': {'type': ['integer', 'null'], 'minimum': 0},
    },
    'additionalProperties': False,
}

capsule_volumes_list = {
    'type': ['array', 'null'],
    'items': {
        'type': 'object',
        'properties': {
            'name': volume_name,
            'cinder': capsule_cinder_volume,
            'file': capsule_file_volume,
            'emptyDir': capsule_empty_dir_volume,
        },
        'additionalProperties': True,
        # Exactly one source. Neither would mount nothing while looking valid;
        # both would leave which one wins up to the order the code happens to
        # check them in.
        'oneOf': [
            {'required': ['name', 'cinder']},
            {'required': ['name', 'file']},
            {'required': ['name', 'emptyDir']},
        ],
    }
}

capsule_spec = {
    'type': ['object'],
    "properties": {
        "containers": capsule_containers_list,
        "volumes": capsule_volumes_list,
        "restartPolicy": capsule_restart_policy,
    },
    "additionalProperties": True,
    "required": ['containers']
}

capsule_architecture = {
    'type': ['string', 'null'],
    'enum': ['x86_64', 'aarch64', 'ppc64le', 's390x', 'riscv64',
             'amd64', 'arm64', None],
}

capsule_template = {
    'type': ['object', 'string', 'unicode'],
    "properties": {
        "kind": capsule_kind,
        "capsuleVersion": capsule_version,
        "metadata": capsule_metadata,
        # NOTE(hongbin): property 'restartPolicy' is deprecated but we keep
        # it here for backward-compatibility. Will remove it after the
        # deprecation period.
        "restartPolicy": capsule_restart_policy,
        "spec": capsule_spec,
        "availabilityZone": availability_zone,
        "architecture": capsule_architecture,
        "nets": nets,
    },
    "additionalProperties": False,
    "required": ['kind', 'spec', 'metadata']
}

neutron_net_id = {
    'type': ['string', 'null'],
    'minLength': 2,
    'maxLength': 255,
    'pattern': '[a-zA-Z0-9][a-zA-Z0-9_.-]'
}

network_name = {
    'type': ['string', 'null'],
    'minLength': 2,
    'maxLength': 255,
    'pattern': '[a-zA-Z0-9][a-zA-Z0-9_.-]'
}

registry_name = {
    'type': ['string'],
    'minLength': 2,
    'maxLength': 255,
    'pattern': '^[a-zA-Z0-9][a-zA-Z0-9_.-]+$'
}

registry_domain = {
    'type': ['string'],
    'minLength': 1,
}

registry_username = {
    'type': ['string'],
    'minLength': 1,
}

registry_password = {
    'type': ['string'],
    'minLength': 1,
}

container_registry = {
    'type': ['string'],
    'minLength': 2,
    'maxLength': 255,
    'pattern': '^[a-zA-Z0-9][a-zA-Z0-9_.-]+$'
}
