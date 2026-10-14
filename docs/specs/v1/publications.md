# Published versions and BibTeX

Carried unchanged from the first v1 (built in M5, PR #23).

## Lookup

`PublicationSource` with a DBLP implementation over its SPARQL endpoint (`sparql.dblp.org`). The HTTP search API
answered this server with a bot challenge on 2026-10-08, on all three mirrors; SPARQL did not, and an exact-title query
returned in about 50 ms with both the CoRR and the conference record. Terms, checked 2026-10-12: public, no account or
key, data CC0, a fixed timeout per query, an unpublished rate limit against aggressive scripting
([announcement](https://blog.dblp.org/2024/09/09/introducing-our-public-sparql-query-service/),
[rate limit discussion](https://github.com/dblp/kg/discussions/7)). litwatch sends one query at a time with a pause
between them and a User-Agent naming litwatch. If access closes or volume grows, the fallback is a local index from
DBLP's dump or a self-hosted dblp SPARQL instance.

## Matching

A DOI the authors gave arXiv names the published record, unless DBLP files it under CoRR. Otherwise locate the preprint record by its arXiv DOI; take
DBLP's title and author list from it; find records with that title, or the paper's current arXiv title, and at least
one shared author; choose the non-CoRR one, the latest if several (a poster or workshop version comes before the full
paper). A paper retitled only at publication is not found this way; the paper page lets the owner paste a DOI or DBLP
key, which is then never overwritten. Checked 2026-10-12 on 50 cs.CL preprints from 2024 whose DOI DBLP knows,
matching alone: 45 right, 5 missed, none wrong.

## When

Library papers are looked up when saved, then monthly for two years, and a single paper on demand when its BibTeX is
copied. An export of many papers never waits on DBLP: it uses stored publications and gives an arXiv entry for the rest
(first v1, decision 25). BibTeX is generated from the published record, or from arXiv metadata when there is none;
papers outside the collection get an arXiv entry from the archive's metadata.
