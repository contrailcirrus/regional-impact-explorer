"""In-memory stand-in for `google.cloud.storage.Client`, for tests.

Implements only the reads `gcs_backend.GcsBackend` makes. Objects are a flat
`{(bucket, name): bytes}` map, so "directories" exist only as name prefixes,
as on GCS.
"""

from collections.abc import Iterator

from google.api_core import exceptions as api_exceptions


class FakeBlob:
  """An object handle; it need not exist yet."""

  def __init__(self, bucket: "FakeBucket", name: str) -> None:
    self.bucket = bucket
    self.name = name

  @property
  def _key(self) -> tuple[str, str]:
    return (self.bucket.name, self.name)

  def exists(self) -> bool:
    return self._key in self.bucket.client.objects

  def download_as_bytes(self) -> bytes:
    try:
      return self.bucket.client.objects[self._key]
    except KeyError:
      raise api_exceptions.NotFound(f"No such object: {self.name}") from None


class FakeBucket:

  def __init__(self, client: "FakeClient", name: str) -> None:
    self.client = client
    self.name = name

  def blob(self, name: str) -> FakeBlob:
    return FakeBlob(self, name)


class FakeBlobIterator:
  """Mimics the client's listing iterator, whose `prefixes` fill as it runs."""

  def __init__(self, blobs: list[FakeBlob], prefixes: set[str]) -> None:
    self._blobs = blobs
    self._pending_prefixes = prefixes
    self.prefixes: set[str] = set()

  def __iter__(self) -> Iterator[FakeBlob]:
    yield from self._blobs
    self.prefixes = self._pending_prefixes


class FakeClient:
  """Holds every object of every bucket.

  Attributes:
    objects: `{(bucket, name): bytes}` for each stored object.
    error: When set, raised by every listing and object read.
  """

  def __init__(self) -> None:
    self.objects: dict[tuple[str, str], bytes] = {}
    self.error: Exception | None = None

  def put(self, path: str, data: bytes | str) -> None:
    """Stores `data` at the `gs://bucket/name` path."""
    bucket, _, name = path.removeprefix("gs://").partition("/")
    if isinstance(data, str):
      data = data.encode("utf-8")
    self.objects[(bucket, name)] = data

  def bucket(self, name: str) -> FakeBucket:
    if self.error is not None:
      raise self.error
    return FakeBucket(self, name)

  def list_blobs(
      self,
      bucket: str,
      prefix: str = "",
      delimiter: str | None = None,
      max_results: int | None = None,
  ) -> FakeBlobIterator:
    if self.error is not None:
      raise self.error
    blobs = []
    prefixes = set()
    for bucket_name, name in sorted(self.objects):
      if bucket_name != bucket or not name.startswith(prefix):
        continue
      rest = name[len(prefix) :]
      if delimiter and delimiter in rest:
        prefixes.add(prefix + rest.split(delimiter)[0] + delimiter)
      else:
        blobs.append(FakeBlob(FakeBucket(self, bucket), name))
    if max_results is not None:
      blobs = blobs[:max_results]
    return FakeBlobIterator(blobs, prefixes)
