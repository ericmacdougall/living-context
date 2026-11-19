"""The keystore is shared by the gateway's threads: calls from any thread work and are serialised."""
import tempfile
import threading
import unittest
from pathlib import Path

from living_context.keystore import Keystore


class KeystoreThreads(unittest.TestCase):
    def test_concurrent_encrypt_and_decrypt_from_other_threads(self):
        with tempfile.TemporaryDirectory() as d:
            ks = Keystore(Path(d) / "tena.db", "tena")  # created on this thread, used from others
            errs, done = [], []

            def work(n):
                try:
                    for i in range(25):
                        leaf = f"leaf{n:02d}{i:03d}"
                        ct, nonce, _ = ks.encrypt(leaf, 1, f"text {n} {i}".encode(), [ks.subject_token(f"p{n}@example.org")])
                        assert ks.decrypt(leaf, 1, ct, nonce) == f"text {n} {i}".encode()
                        done.append(leaf)
                except Exception as e:  # pragma: no cover
                    errs.append(repr(e))

            ts = [threading.Thread(target=work, args=(n,)) for n in range(6)]
            [t.start() for t in ts]
            [t.join(60) for t in ts]
            self.assertEqual(errs, [])
            self.assertEqual((len(done), ks.count_keys()), (150, 150))
            ks.close()


if __name__ == "__main__":
    unittest.main()
