"""arXiv HTML to markdown: the shapes the REPL helpers rely on."""

from litwatch.text import html_to_markdown

HTML = """
<html><body><article class="ltx_document">
<h1 class="ltx_title ltx_title_document">A Paper</h1>
<div class="ltx_authors"><span>Someone</span></div>
<div class="ltx_abstract"><h6 class="ltx_title ltx_title_abstract">Abstract</h6>
<p class="ltx_p">We probe <math alttext="x^{2}" class="ltx_Math" display="inline"><semantics><msup><mi>x</mi><mn>2</mn></msup>
<annotation encoding="application/x-tex">x^{2}</annotation></semantics></math> things
<span class="ltx_note"><sup class="ltx_note_mark">1</sup><span class="ltx_note_outer"><span class="ltx_note_content">
<sup class="ltx_note_mark">1</sup><span class="ltx_note_type">footnotetext: </span>Code: https://example.org/repo</span></span></span>.</p></div>
<section class="ltx_section"><h2 class="ltx_title ltx_title_section"><span class="ltx_tag">1 </span>Intro</h2>
<p class="ltx_p">See <cite class="ltx_cite">(<a class="ltx_ref" href="#bib.bib1" title="noise">Doe, 2020</a>)</cite> and
Figure <a class="ltx_ref" href="#S1.F1" title="Figure 1 ‣ long">1</a>.</p>
<table class="ltx_equation ltx_eqn_table"><tr class="ltx_equation"><td class="ltx_eqn_cell"></td>
<td><math alttext="y=mx+b" display="block"><semantics><mi>y</mi></semantics></math></td>
<td class="ltx_eqn_eqno">(1)</td></tr></table>
<figure class="ltx_table"><figcaption><span class="ltx_tag">Table 1: </span>Results for <math alttext="n=5" display="inline"><mi>n</mi></math></figcaption>
<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table></figure>
</section>
<section class="ltx_bibliography"><h2 class="ltx_title">References</h2><ul class="ltx_biblist">
<li class="ltx_bibitem"><span class="ltx_bib_author">J. Doe</span> <span class="ltx_bib_title">Old Work</span>. External Links: Link Cited by: §1.</li>
</ul></section>
<div class="ltx_rdf">junk</div>
</article></body></html>
"""


def test_markdown_keeps_math_headings_refs_and_drops_noise():
    md = html_to_markdown(HTML)
    head = " ".join(md.split("\n## 1 Intro")[0].split())
    assert (
        head
        == "# A Paper ## Abstract We probe $x^{2}$ things [footnote: Code: https://example.org/repo] ."
    )
    assert "\n## 1 Intro\n" in md
    assert "See (Doe, 2020) and" in md and "Figure 1." in md and "noise" not in md
    assert "$$\ny=mx+b \\tag{1}\n$$" in md
    assert "*Table 1: Results for $n=5$*" in md
    assert "| a | b |" in md
    assert "\n## References\n\n- J. Doe Old Work.\n" in md
    assert "Someone" not in md and "junk" not in md
