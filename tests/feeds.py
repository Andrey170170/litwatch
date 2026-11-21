"""arXiv's RSS listing as Atom: a feed and its entries, as intake reads them."""


def entry(paper_id="2609.11934v1", announcement="new", abstract="An abstract."):
    return f"""<entry>
      <id>oai:arXiv.org:{paper_id}</id><title>A paper</title>
      <summary>arXiv:{paper_id} Announce Type: {announcement}
Abstract: {abstract}</summary>
      <arxiv:announce_type>{announcement}</arxiv:announce_type>
      <published>2026-09-14T00:00:00Z</published>
      <updated>2026-09-14T20:00:00Z</updated>
      <dc:creator>Smith, Jr., Alice, Bob Jones</dc:creator>
      <category term="cs.CL"/><category term="cs.AI"/>
    </entry>"""


def feed(entries=""):
    return f"""<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:dc="http://purl.org/dc/elements/1.1/">
      <updated>2026-09-14T20:00:00Z</updated>{entries}</feed>""".encode()
