You are checking whether an answer is supported by the source text it was based on.

Question:
<question>
{question}
</question>

Answer:
<answer>
{answer}
</answer>

Text of the sources the answer cites (everything the assistant's tools returned for them). A passage from the search
index starts with a line "[index label: <file title> > <section or Page N>]": the label the assistant saw with it.
"Page N" counts the pages of the file (the PDF page), which often differs from a page number printed in the text.
<sources>
{sources}
</sources>

Decide whether every factual claim in the answer is supported by the source text. Arithmetic on figures that
appear in the sources (a ratio, a difference, a sum) counts as supported if it is done correctly. General
knowledge that any reader would accept without a source (what a 10-K is) doesn't need support. A claim that the
sources don't contain, or that contradicts them, makes the answer ungrounded. An answer that only says the
information wasn't found is grounded.

A pointer to where something was found (a section or page number, a table or note number, the name of the file) is
not a claim about the content. A page is right if it is the PDF page in a passage's index label or a page printed in
its text (they often differ); a section is right if the label or text names it, in any wording. A passage with no
label doesn't show where the index put it, so a pointer missing from its text is not wrong. A pointer is wrong only
if its passage has a label and the pointer matches neither the label nor a location printed in the text.

Return `grounded` (true/false), `unsupported` (the unsupported claims, quoted briefly; empty if none) and a
one-sentence `reason`.
