# Sources and attribution

`sotif/ontology.nt` extends the SOTIF scenario ontology with instances and relations extracted by an LLM
(`sotif/build.py`) from the documents below. `sotif/provenance.json` records, for each added relation, the
source id and a short evidence note (paraphrase or brief quotation). Content was changed: facts were
restated as RDF triples and evidence notes were shortened or translated.

| id | title | authors | year | license |
|---|---|---|---|---|
| arxiv-2303.04037 | [Discovery of Perception Performance Limiting Triggering Conditions in Automated Driving](https://arxiv.org/abs/2303.04037) | Adee, Gansch, Liggesmeyer, Glaeser, Drews | 2023 | CC BY 4.0 |
| arxiv-2303.04029 | [Systematic Modeling Approach for Environmental Perception Limitations in Automated Driving](https://arxiv.org/abs/2303.04029) | Adee, Gansch, Liggesmeyer | 2023 | CC BY 4.0 |
| arxiv-2407.21569 | [Analysis of Functional Insufficiencies and Triggering Conditions to Improve the SOTIF of an MPC-based Trajectory Planner](https://arxiv.org/abs/2407.21569) | Conrad, Schildbach | 2024 | CC BY 4.0 |
| arxiv-2408.14481 | [Formalization of Operational Domain and Operational Design Domain for Automated Vehicles](https://arxiv.org/abs/2408.14481) | Shakeri | 2024 | CC BY 4.0 |
| arxiv-2503.00709 | [ICanC: Improving Camera-based Object Detection and Energy Consumption in Low-Illumination Environments](https://arxiv.org/abs/2503.00709) | Ma, Zhong, Shi | 2025 | CC BY 4.0 |
| arxiv-2509.06333 | [Multi-Modal Camera-Based Detection of Vulnerable Road Users](https://arxiv.org/abs/2509.06333) | Brown, Perez, Shan, Worrall | 2025 | CC BY 4.0 |
| arxiv-2210.10233 | [Vision-Based Robust Lane Detection and Tracking under Different Challenging Environmental Conditions](https://arxiv.org/abs/2210.10233) | Sultana, Ahmed, Paul, Islam, Ahmad | 2022 | CC BY 4.0 |

License texts: https://creativecommons.org/licenses/by/4.0/

**Review against the source text (2026-11).** Every added relation and the description of every added instance was
checked against the text of the cited paper. Relations the paper does not state (a sensor or result the paper does not
mention, a reversed direction, a value the paper does not use, or an assumption of the paper restated as a finding)
were removed, and descriptions were cut to what the paper says, with its section or TC/FI number. Of the relations
`build.py` had added, 36 remain. While reviewing, the last three papers above were found to state five of the kept
relations more directly than the paper `build.py` had cited, so those relations now cite them; these sources were added
by hand, not by `build.py`. After the review no relation cites arxiv-2408.14481; it stays in `sources.json` as a build
input.

The original 104 instances were drafted for this sample with an AI assistant from concepts named in ISO 21448 (SOTIF),
ISO 34502 and Euro NCAP (for example triggering condition, functional insufficiency, the SOTIF scenario areas),
without source documents. No text from those standards is included.

In `sotif/provenance.json`, an instance's `sources` lists the sources of the relations that use it; it is not the
source of the instance's description. The original 104 instances (above) keep their AI-drafted descriptions even when
`sources` is filled in.
