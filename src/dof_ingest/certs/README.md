# Missing intermediate certificates

These are **not** custom or self-signed certificates, and adding them does not
weaken TLS verification. They are the public intermediate CAs that two of the
target hosts fail to send.

## The problem

`www.dof.gob.mx` and `www.datos.gob.mx` both serve an incomplete certificate
chain — they present the leaf without the intermediate that links it to a
trusted root. `www.dof.gob.mx` is the more entertaining case: it sends its leaf
certificate **twice**.

```
$ echo | openssl s_client -connect www.dof.gob.mx:443 -servername www.dof.gob.mx
Certificate chain
 0 s:CN=dof.gob.mx
   i:CN=Go Daddy Secure Certificate Authority - G2
 1 s:CN=dof.gob.mx          <-- the leaf again, where the intermediate should be
   i:CN=Go Daddy Secure Certificate Authority - G2
```

`curl` and every browser accept this, which is why nobody at SEGOB has noticed:
macOS Secure Transport and Windows CryptoAPI follow the certificate's
Authority Information Access (AIA) extension and download the missing
intermediate on the fly. OpenSSL — and therefore Python, `httpx`, `requests`,
and your CI runner — does not do AIA chasing, and fails with:

```
[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate
```

## The fix, and the fix we did not use

The tempting fix is `verify=False`. That turns off certificate verification
entirely and makes the scraper trivially interceptable — an unreasonable
trade for a cosmetic server misconfiguration.

Instead we supply the link the servers omit. Each file below was downloaded
from the URL named in the *server's own* AIA extension, so the source is the
certificate itself rather than a third party:

| File | Subject | Issued by | AIA source |
|---|---|---|---|
| `godaddy-g2.pem` | Go Daddy Secure Certificate Authority - G2 | Go Daddy Root Certificate Authority - G2 | `http://certificates.godaddy.com/repository/gdig2.crt` |
| `le-e8.pem` | Let's Encrypt E8 | ISRG Root X2 | `http://e8.i.lencr.org/` |

Both issuers are already in `certifi`. We are adding the middle of a chain
whose root we already trusted, not a new trust anchor of our own invention.

Verify them yourself:

```bash
openssl x509 -in src/dof_ingest/certs/godaddy-g2.pem -noout -subject -issuer
openssl x509 -in src/dof_ingest/certs/le-e8.pem      -noout -subject -issuer
```

## Maintenance

These expire. When one does, TLS starts failing again and
`dof-ingest research` will say so loudly. Re-download from the AIA URL in the
table above. The better outcome is that SEGOB fixes its chain and this
directory can be deleted — see `docs/DECISIONS.md`.
