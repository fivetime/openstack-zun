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

"""Choose between eventlet and native threading, once, at process start.

Every zun binary under zun.cmd imports this before anything else. With
OS_ZUN_DISABLE_EVENTLET_PATCHING unset (or false) the process is monkey
patched by eventlet as it always was. With it set to true nothing is
patched: the services run on native threads, oslo.service on its
threading backend and oslo.messaging on its threading executor.

The choice is made here and nowhere else; the rest of zun asks
concurrency_mode_threading() rather than looking at the environment, so
that a process can not end up half in one mode and half in the other.
"""

import os

ENV_VAR = 'OS_ZUN_DISABLE_EVENTLET_PATCHING'

_MODE = None


def _threading_requested():
    return os.environ.get(ENV_VAR, '').strip().lower() in ('1', 'true', 'yes')


def patch():
    """Apply the mode the environment asks for. Idempotent."""
    global _MODE
    if _MODE is not None:
        return

    # NOTE: nothing imported above this point may need patching; the
    # oslo.service import is after eventlet.monkey_patch() on purpose.
    if _threading_requested():
        import oslo_service.backend as service_backend
        # Raises if something already picked the eventlet backend, which
        # is the half-and-half process this module is here to prevent.
        service_backend.init_backend(service_backend.BackendType.THREADING)
        _MODE = 'threading'
    else:
        import eventlet
        eventlet.monkey_patch()
        import oslo_service.backend as service_backend
        service_backend.init_backend(service_backend.BackendType.EVENTLET)
        _MODE = 'eventlet'


def concurrency_mode_threading():
    """True when this process runs on native threads.

    Processes that never went through patch() (the API under a WSGI
    server, the CNI daemon, the unit tests) are not monkey patched, so
    for them the answer is also true.
    """
    return _MODE != 'eventlet'
