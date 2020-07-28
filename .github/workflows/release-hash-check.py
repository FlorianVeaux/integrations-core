from fnmatch import fnmatch
import sys
import json
import hashlib


def compute_sha256(filename):
    sha256_hash = hashlib.sha256()
    with open(filename, "rb") as f:
        # Read and update hash string value in blocks of 4K
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


updated_link_files = [f for f in sys.argv[1:] if fnmatch(f, '.in-toto/tag.*.link')]
if len(updated_link_files) < 0:
    raise Exception("The release-hash-check should only run upon modification of a link file.")
if len(updated_link_files) > 1:
    raise Exception("There should never be two different link files modified at the same time.")

with open(updated_link_files[0], 'r') as f:
    link_file = json.load(f)

products = link_file['signed']['products']

for product, signatures in products.iteritems():
    expected_sha = signatures['sha256']
    if expected_sha != compute_sha256(product):
        raise Exception(
            f"File {product} currently has a different sha that what has been signed."
            f"Is your branch up to date with master?"
        )

print(f"Link file {link_file} has valid signatures.")