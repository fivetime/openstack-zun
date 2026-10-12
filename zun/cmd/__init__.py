# Copyright 2017 Fujitsu Ltd.
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

# Eventlet or native threads: decided here, before any other zun import,
# by OS_ZUN_DISABLE_EVENTLET_PATCHING (see zun.monkey_patch).

from zun import monkey_patch

monkey_patch.patch()
