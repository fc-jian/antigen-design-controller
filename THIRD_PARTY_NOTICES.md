# Third-party components and data

The repository's application code is distributed under [MIT](LICENSE). This license does not relicense dependencies, models, weights or data.

- The bundled `src/antigen_design/web/vendor/3Dmol-min.js` is from **3dmol 2.5.5**. Its full upstream license and incorporated GLmol/Three.js/jQuery notices are preserved in [3Dmol-LICENSE.txt](src/antigen_design/web/vendor/3Dmol-LICENSE.txt). Do not remove these notices when redistributing the viewer.
- The benchmark is public **PDB 1UBQ**, attributed to its depositors and [RCSB PDB](https://www.rcsb.org/structure/1UBQ). The unmodified mmCIF and its source URL/SHA-256 are in [the benchmark directory](projects/1ubq/inputs/1ubq.provenance.json). It is a structural engineering example, not a claim about antigen efficacy.
- NumPy, Biopython, PyYAML and test/build tooling are installed separately under their own terms.
- [Foundry](https://github.com/RosettaCommons/foundry), [Protenix](https://github.com/bytedance/Protenix), [ColabFold](https://github.com/sokrypton/ColabFold) and [MMseqs2](https://github.com/soedinglab/MMseqs2) are optional backend dependencies, not vendored models. Their code, weights and databases may have different licenses from one another.
- As checked for this release, the Protenix upstream README states that **Protenix v2 weights are not released under an open-source license**. Deployers must obtain suitable authorization separately. No v2 checkpoint or model/database download archive is part of this distribution.

This release contains a clean export of generic application modules, not the development workspace's history or private infrastructure. Historical task archives, user-specific manifests and target-specific biological cases are excluded.
