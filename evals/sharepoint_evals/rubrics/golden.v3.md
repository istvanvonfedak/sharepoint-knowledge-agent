You are grading an assistant's answer to a question about an organisation's documents.

Question (language: {language}):
<question>
{question}
</question>

Reference answer, reviewed by people who know the documents:
<reference>
{reference}
</reference>

Facts a correct answer must state, one per line:
<facts>
{facts}
</facts>

Assistant's answer:
<answer>
{answer}
</answer>

Text of the sources the answer cites. Each passage starts with the link of the file it comes from. A passage from the
search index then has a line "[index label: <file title> > <section or Page N>]": the label the assistant saw with
it. "Page N" in a label counts the pages of the file (the PDF page), which often differs from a page number printed in
the passage text, such as 3-16. Some passages (a whole file, an older run) have no label.
<sources>
{sources}
</sources>

Grade three things:
- `facts`: for each fact above, in order, true if the answer states it correctly (in any wording or language),
  false if it is missing or wrong.
- `grounded`: true if every factual claim in the answer is supported by the source text. Apply these rules:
  - Correct arithmetic or unit conversion on figures in the sources counts as supported.
  - A pointer to where something was found (a section heading, a section or paragraph number, a page number, a
    table or figure number, the name of the file) is not a claim about the content. It is right if it matches the
    passage's index label or its text: a PDF page from the label ("page 230", "PDF page 230") and a different page
    number printed in the text ("3-16") are both right. Never mark an answer ungrounded because a pointer is missing
    from the passage text, or because it gives the file's page rather than the printed one. Mark it ungrounded only
    if the pointer matches neither the label nor the text, or if it is used to support a claim the source doesn't
    make (for example "section 4 says X" when the passages say nothing of X).
  - A qualifier is part of the claim: the unit or basis of a number (working or calendar days, per year or per
    month, lb-ft or lb-in), who or what it applies to (a model, a region, a staff group), and any condition or
    exception. If the answer states a qualifier the passages don't give, or one that differs from theirs, it is
    unsupported.
  - A claim about what a document does not say, or a closing remark that only advises the reader to check
    elsewhere ("confirm with HR", "other models may differ"), is not a factual claim to check.
  - A claim the passages contradict, or that no passage supports, is unsupported even if it is true in general
    or you know it to be true. An answer that gives no sources is not grounded: with "(no source text)" in
    `<sources>`, any factual claim is unsupported.
- `language_ok`: true if the answer is written in the question's language ({language}).

Also give a one-sentence `reason`. If `grounded` is false, name the unsupported claim in it.
