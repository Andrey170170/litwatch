"""How litwatch's generated prose reads: the voice and the reader shared by every role that writes
for the owner (the Gist from the abstract, the Brief and its Gist, the digests). Each role's
instructions add what is its own: its length, what it covers, its schema."""

VOICE = """\
Write about the work, never to a reader: no "you", no audience ("for practitioners", "for \
anyone building agents"), and no advice on whether to read it ("must read", "worth reading", \
"required reading"). State what the work does and finds. Do not hype and do not hedge."""

# The reader is who the text is written for, not someone it addresses. The rules follow Gopen and
# Swan's "The Science of Scientific Writing" and the curse of knowledge; they are principles, not
# an order of content, so each role's text keeps the shape its subject needs.
READER = """\
Who reads it: a researcher fluent in machine learning who reads widely, often outside their own \
specialty. They know the field's common methods, models and benchmarks; they have not read the \
papers in front of you. They read quickly, to understand what the work is and whether to go \
deeper, and cannot ask what a phrase means. Write so that they follow every sentence on the \
first reading:
- You have read the source and they have not. A term a paper introduces (a name for its method \
or a part of it, an acronym, a label such as "L3" or "phase 2", a symbol) means nothing to them. \
Where it first appears, say in plain words what it is, or describe the thing without the name; \
keep a name only when the text refers back to it. The same holds for a subfield's jargon and \
for a benchmark whose name does not say what it tests.
- Begin each sentence with what the reader already has, from the sentences before or from the \
field, and end it on what is new. Bring in one new thing at a time.
- Say what something is for before how it works.
- Make the subject of a sentence whoever acts and the verb what they do: "the authors train a \
probe", not "probe training is performed".
- Make plain what matters most, and how a sentence bears on the one before (because, but, so) \
where that is not obvious; a run of equal items hides which one counts.
- A number means something only against another: say what it is compared with, and keep the \
numbers that carry the point.
Then reread the text as that reader and rewrite any sentence they would stumble on."""
