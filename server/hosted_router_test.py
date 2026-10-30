"""Tests `server.router` mounted in another application, reading from GCS.

This is how a host such as contrails-api serves the dashboard API: the router
under a path prefix, the domain exception handlers registered on the host, no
startup preload, and published artifacts read from a `gs://` data root with
the GCS client instead of Apache Beam.
"""

import json
from unittest import mock

from absl import flags
from absl.testing import absltest
from absl.testing import parameterized
import fastapi
from fastapi import testclient

from common import fs
from common import gcs_backend
from common import gcs_testing
from server import data_loader
from server import server
from server import testdata_fixture

_PREFIX = "/internal/regional"
_DATA_DIR = "gs://results"


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


class HostedRouterTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.storage = gcs_testing.FakeClient()
    for relative_path, payload in testdata_fixture.build_fixture().items():
      self.storage.put(f"{_DATA_DIR}/{relative_path}", json.dumps(payload))
    self.enter_context(
        mock.patch.object(
            gcs_backend.storage, "Client", return_value=self.storage
        )
    )
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", _DATA_DIR))
    self.enter_context(mock.patch.object(data_loader, "_active_dataset", None))
    data_loader.configure_storage()
    self.addCleanup(fs.register_filesystem_backend, None)
    data_loader.configure_dataset(testdata_fixture.DATASET)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)

    host = fastapi.FastAPI()
    host.include_router(server.router, prefix=_PREFIX)
    server.register_exception_handlers(host)
    self.client = testclient.TestClient(host)

  def test_years_are_discovered_in_gcs(self):
    response = self.client.get(f"{_PREFIX}/years")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(
        response.json()["years"], sorted(testdata_fixture.YEAR_FACTOR)
    )

  def test_country_metrics_are_read_from_gcs(self):
    year = max(testdata_fixture.YEAR_FACTOR)
    response = self.client.get(f"{_PREFIX}/countries/{year}")
    self.assertEqual(response.status_code, 200)
    self.assertNotEmpty(response.json())

  @parameterized.parameters(
      # DataValidationError: not a four-digit year.
      ("/countries/20x6", 400),
      # UnknownMetricError.
      (f"/rankings/{max(testdata_fixture.YEAR_FACTOR)}/no_such_metric", 404),
  )
  def test_domain_errors_map_to_their_status_codes(self, path, status_code):
    response = self.client.get(f"{_PREFIX}{path}")
    self.assertEqual(response.status_code, status_code, response.text)

  @parameterized.parameters("/years", "/", "/index.html")
  def test_nothing_is_served_outside_the_prefix(self, path):
    self.assertEqual(self.client.get(path).status_code, 404)


class ConfigureStorageTest(absltest.TestCase):

  def test_local_data_root_keeps_the_standard_library(self):
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", "/data"))
    self.enter_context(mock.patch.object(fs, "_backend", None))
    data_loader.configure_storage()
    self.assertIsNone(fs._backend)


if __name__ == "__main__":
  absltest.main()
