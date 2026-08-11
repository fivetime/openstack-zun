.. -*- rst -*-

=======================
 Manage Capsules (Pods)
=======================

Lists, creates, shows details for, and deletes capsules.

Capsules is a collection of containers that can run on a host.
It is basically a Zun implementation of Kubernetes Pod.
Containers inside a capsule share the Neutron network and Cinder volumes.

Create new capsule
==================

.. rest_method:: POST /v1/capsules/

Create new capsule.

Response Codes
--------------

.. rest_status_code:: success status.yaml

   - 202

.. rest_status_code:: error status.yaml

   - 400
   - 401
   - 403

Request
-------

Capsule Template
^^^^^^^^^^^^^^^^

.. rest_parameters:: parameters.yaml

  - template: capsule-template-request
  - kind: capsule-kind-request
  - metadata: capsule-metadata-request
  - metadata.labels: capsule-labels-request
  - metadata.name: capsule-name-request
  - metadata.annotations: capsule-annotations-request
  - spec: capsule-spec-request

.. _create-capsule-spec:

Capsule Spec
^^^^^^^^^^^^

.. rest_parameters:: parameters.yaml

  - containers: capsule-containers-request
  - initContainers: capsule-initContainers-request
  - restartPolicy: capsule-restartPolicy-request
  - volumes: capsule-volumes-request

.. _create-capsule-container:

Capsule Container
^^^^^^^^^^^^^^^^^

.. rest_parameters:: parameters.yaml

  - image: image
  - command: capsule-command-request
  - args: capsule-args-request
  - env: environment-request
  - name: capsule-container-name-request
  - ports: capsule-ports-request
  - ports.containerPort: capsule-ports-containerPort
  - ports.protocol: capsule-ports-protocol
  - resources: capsule-resources-request
  - resources.cpu: capsule-resources-cpu
  - resources.memory: capsule-resources-memory
  - volumeMounts: capsule-volumeMounts-request
  - volumeMounts.name: capsule-volumeMounts-name
  - volumeMounts.mountPath: capsule-volumeMounts-mountPath
  - workDir: workdir-request

.. _create-capsule-volume:

Capsule Volume
^^^^^^^^^^^^^^

.. rest_parameters:: parameters.yaml

  - name: capsule-volume-name-request
  - cinder: capsule-cinder-request
  - cinder.volumeID: capsule-cinder-volumeID
  - cinder.size: capsule-cinder-size
  - cinder.autoRemove: capsule-cinder-autoRemove

Request Example
---------------

.. literalinclude:: samples/capsule-create-req.json
   :language: javascript

Response
--------

Capsule
^^^^^^^

.. rest_parameters:: parameters.yaml

  - addresses: capsule-addresses
  - containers: capsule-containers-create
  - cpu: capsule-cpu
  - memory: capsule-memory
  - uuid: capsule-uuid
  - name: capsule-name
  - labels: capsule-labels
  - annotations: capsule-annotations
  - restart_policy: capsule-restart_policy
  - created_at: created_at
  - updated_at: updated_at
  - user_id: capsule-user_id
  - project_id: capsule-project_id
  - status: capsule-status
  - status_reason: capsule-status_reason
  - links: capsule-links

.. _create-capsule-container-response:

Capsule Container
^^^^^^^^^^^^^^^^^

.. rest_parameters:: parameters.yaml

  - addresses: addresses
  - image: image
  - labels: labels
  - disk: disk
  - security_groups: security_groups
  - task_state: task_state
  - user_id: user_id
  - uuid: container_uuid
  - hostname: hostname
  - environment: environment
  - memory: memory
  - project_id: container-project_id
  - status: status
  - environment: environment
  - workdir: workdir
  - healthcheck: healthcheck
  - auto_remove: auto_remove
  - auto_heal: auto_heal
  - cpu_policy: cpu_policy
  - image_driver: image_driver
  - status_detail: status_detail
  - status_reason: status_reason
  - name: name
  - restart_policy: restart_policy
  - cpu: cpu
  - command: command
  - registry_id: registry_id
  - ports: ports
  - interactive: interactive
  - tty: tty

Response Example
----------------

.. literalinclude:: samples/capsule-create-resp.json
   :language: javascript

List all capsules
=================

.. rest_method:: GET /v1/capsules/

List all available capsules in Zun.

Response Codes
--------------

.. rest_status_code:: success status.yaml

   - 200

.. rest_status_code:: error status.yaml

   - 401
   - 403

Request
-------

This request does not contain anything in the request body.

Response
--------

Capsule
^^^^^^^

.. rest_parameters:: parameters.yaml

  - addresses: capsule-addresses
  - containers: capsule-containers-list
  - cpu: capsule-cpu
  - memory: capsule-memory
  - uuid: capsule-uuid
  - name: capsule-name
  - labels: capsule-labels
  - annotations: capsule-annotations
  - restart_policy: capsule-restart_policy
  - created_at: created_at
  - updated_at: updated_at
  - user_id: capsule-user_id
  - project_id: capsule-project_id
  - status: capsule-status
  - status_reason: capsule-status_reason
  - links: capsule-links

.. _list-capsule-container-response:

Capsule Container
^^^^^^^^^^^^^^^^^

.. rest_parameters:: parameters.yaml

  - addresses: addresses
  - image: image
  - labels: labels
  - disk: disk
  - security_groups: security_groups
  - task_state: task_state
  - user_id: user_id
  - uuid: container_uuid
  - hostname: hostname
  - environment: environment
  - memory: memory
  - project_id: container-project_id
  - status: status
  - environment: environment
  - workdir: workdir
  - healthcheck: healthcheck
  - auto_remove: auto_remove
  - auto_heal: auto_heal
  - cpu_policy: cpu_policy
  - image_driver: image_driver
  - status_detail: status_detail
  - status_reason: status_reason
  - name: name
  - restart_policy: restart_policy
  - cpu: cpu
  - command: command
  - registry_id: registry_id
  - ports: ports
  - interactive: interactive
  - tty: tty

Response Example
----------------

.. literalinclude:: samples/capsule-get-all-resp.json
   :language: javascript

Show details of a capsule
=========================

.. rest_method:: GET /v1/capsules/{capsule_ident}

Get all information of a capsule in Zun.

Response Codes
--------------

.. rest_status_code:: success status.yaml

   - 200

.. rest_status_code:: error status.yaml

   - 401
   - 403
   - 404

Request
-------

.. rest_parameters:: parameters.yaml

  - capsule_ident: capsule_ident

Response
--------

Capsule
^^^^^^^

.. rest_parameters:: parameters.yaml

  - addresses: capsule-addresses
  - containers: capsule-containers-show
  - cpu: capsule-cpu
  - memory: capsule-memory
  - uuid: capsule-uuid
  - name: capsule-name
  - labels: capsule-labels
  - annotations: capsule-annotations
  - restart_policy: capsule-restart_policy
  - created_at: created_at
  - updated_at: updated_at
  - user_id: capsule-user_id
  - project_id: capsule-project_id
  - status: capsule-status
  - status_reason: capsule-status_reason
  - links: capsule-links

.. _show-capsule-container-response:

Capsule Container
^^^^^^^^^^^^^^^^^

.. rest_parameters:: parameters.yaml

  - addresses: addresses
  - image: image
  - labels: labels
  - disk: disk
  - security_groups: security_groups
  - task_state: task_state
  - user_id: user_id
  - uuid: container_uuid
  - hostname: hostname
  - environment: environment
  - memory: memory
  - project_id: container-project_id
  - status: status
  - environment: environment
  - workdir: workdir
  - healthcheck: healthcheck
  - auto_remove: auto_remove
  - auto_heal: auto_heal
  - cpu_policy: cpu_policy
  - image_driver: image_driver
  - status_detail: status_detail
  - status_reason: status_reason
  - name: name
  - restart_policy: restart_policy
  - cpu: cpu
  - command: command
  - registry_id: registry_id
  - ports: ports
  - interactive: interactive
  - tty: tty

Response Example
----------------

.. literalinclude:: samples/capsule-show-resp.json
   :language: javascript

Delete a capsule
================

.. rest_method:: DELETE /v1/capsules/{capsule_ident}

Delete a capsule.

Response Codes
--------------

.. rest_status_code:: success status.yaml

   - 204

.. rest_status_code:: error status.yaml

   - 401
   - 403
   - 404
   - 409

Request
-------

.. rest_parameters:: parameters.yaml

  - capsule_ident: capsule_ident

Response
--------

This request does not return anything in the response body.


Read a capsule container's logs
===============================

.. rest_method:: GET /v1/capsules/{capsule_ident}/logs

Read the output of one container in a capsule.

A capsule holds several containers, so which one is being asked for has to be
named; without it the only sensible answer would be the first, and a caller
that meant another would silently read the wrong one.

Response Codes
--------------

.. rest_status_code:: success status.yaml

   - 200

.. rest_status_code:: error status.yaml

   - 400
   - 401
   - 403
   - 404

Request
-------

.. rest_parameters:: parameters.yaml

  - capsule_ident: capsule_ident
  - container: capsule_container_ident
  - stdout: stdout
  - stderr: stderr
  - timestamps: timestamps
  - tail: tail
  - since: since

Response
--------

The log as a string, not in json format. A container that has not started yet
has no output, which is answered as empty rather than as an error.


Run a command in a capsule container
====================================

.. rest_method:: POST /v1/capsules/{capsule_ident}/execute

Run a command in one container of a capsule.

Two shapes, chosen by ``run`` and ``interactive``, and they are mutually
exclusive: one waits for the command to finish and answers with its output,
the other hands back a session to type into.

Response Codes
--------------

.. rest_status_code:: success status.yaml

   - 200

.. rest_status_code:: error status.yaml

   - 400
   - 401
   - 403
   - 404
   - 409

Request
-------

.. rest_parameters:: parameters.yaml

  - capsule_ident: capsule_ident
  - container: capsule_container_ident
  - command: command
  - run: run
  - interactive: interactive

Response
--------

With ``run=true``: the command's output and exit code.

With ``interactive=true``: ``proxy_url``, a websocket address carrying a token
that admits exactly one session. It names the node holding the capsule -- the
runtime serves the stream on that node's loopback, so only the proxy running
there can reach it. The session speaks the five-channel remote command
protocol: the first byte of every frame is the channel (0 stdin, 1 stdout,
2 stderr, 3 error, 4 resize).


Read a capsule's resource usage
===============================

.. rest_method:: GET /v1/capsules/{capsule_ident}/stats

Report what every container in the capsule is using.

Per container rather than one figure for the capsule: that is the shape the
runtime accounts in, and it is what answering "which container is using the
memory" needs. Summing is cheap for a caller that wants a total; splitting one
is not possible.

Response Codes
--------------

.. rest_status_code:: success status.yaml

   - 200

.. rest_status_code:: error status.yaml

   - 400
   - 401
   - 403
   - 404

Request
-------

.. rest_parameters:: parameters.yaml

  - capsule_ident: capsule_ident

Response
--------

``stats``, a list with one entry per container:
``container_id``, ``name``, ``timestamp`` (nanoseconds),
``cpu_usage_core_nanoseconds``, ``memory_working_set_bytes`` and
``memory_usage_bytes``.

CPU is cumulative, not a rate, and is reported as the runtime counts it. Two
readings make a rate; one does not -- and the counter resets when a container
restarts, so a rate computed across one is wrong.


Replace a capsule file volume's contents
========================================

.. rest_method:: POST /v1/capsules/{capsule_ident}/update_file

Rewrite one of the capsule's file volumes in place.

For content that has to change while the capsule runs, which a capsule
otherwise has no way to express: it is built once and cannot be changed. The
case that forces this is a service account token, which expires. The
alternatives are worse -- recreating the capsule restarts the workload and
takes its address with it, and running a command inside the container to
rewrite the file needs a shell, which the better images do not have.

The file is rewritten where it is, so a container reading it sees the new
contents without anything being remounted. Only a local file volume can be
rewritten; a Cinder volume is a block device and answers that it is not a file.

Response Codes
--------------

.. rest_status_code:: success status.yaml

   - 200

.. rest_status_code:: error status.yaml

   - 400
   - 401
   - 403
   - 404

Request
-------

.. rest_parameters:: parameters.yaml

  - capsule_ident: capsule_ident
  - path: container_path
  - contents: file_contents

Response
--------

``updated``, the path that was rewritten.
