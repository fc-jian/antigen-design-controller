# 1UBQ integration benchmark

The unmodified [1ubq.cif](inputs/1ubq.cif) is human ubiquitin from [RCSB PDB](https://www.rcsb.org/structure/1UBQ). Its download URL and SHA-256 are in [provenance](inputs/1ubq.provenance.json). The expected parsed protein has 76 residues. Public structural attribution inside the mmCIF is retained.

Examples: [fixed backbone](configs/ubiquitin.yaml), [RFD3](configs/ubiquitin-p3.yaml), [regional matrix](configs/ubiquitin-matrix.yaml), [screening policy](configs/screening-example.json). All input/output paths are relative to each scientific configuration. Set a new output directory before each changed run.

No historical runs, scheduler receipts, predictions or acceptance archives are included. Use the [deployment tutorial](../../README.md) to configure your own backend and [verification statement](../../docs/verification.md) to understand what this release actually checked.
