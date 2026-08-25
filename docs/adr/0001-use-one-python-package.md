# Use one Python package

Build 1.0 as one Python package using pyfuse3 with Trio, HTTPX, stdlib SQLite, SecretStorage, and a small Unix-socket protocol. Go, Rust, and C reduce some runtime dependencies but require more cross-language desktop glue or more manual asynchronous lifecycle code; Python lets the FUSE daemon, CLI, and Nautilus adapter share the domain implementation while Arch supplies the native libraries.
