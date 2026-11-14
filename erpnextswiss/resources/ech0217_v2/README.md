# Official offline schema closure

Unmodified official eCH-0217 2.0.0 and its transitive XSD imports. `sources.json`
records the official source URL and SHA-256 of every schema. These files are
packaged in both wheel and source distribution. XML validation resolves imports
only to this directory; runtime network access and external entity expansion are
disabled. The archive tests verify the complete checksummed closure.
