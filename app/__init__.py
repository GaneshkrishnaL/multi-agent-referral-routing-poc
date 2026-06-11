# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lazy export (PEP 562): `from app import app` still works for the ADK server,
# but importing deterministic submodules (app.routing, app.clinical_data) no
# longer drags in the full ADK graph or requires GCP credentials — keeping the
# offline routing validator and unit tests genuinely offline.
__all__ = ["app"]


def __getattr__(name):
    if name == "app":
        from .agent import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
