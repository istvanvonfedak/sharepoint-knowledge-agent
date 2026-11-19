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

Text of the sources the answer cites:
<sources>
{sources}
</sources>

Grade three things:
- `facts`: for each fact above, in order, true if the answer states it correctly (in any wording or language),
  false if it is missing or wrong.
- `grounded`: true if every factual claim in the answer is supported by the source text (correct arithmetic on
  figures in the sources counts as supported).
- `language_ok`: true if the answer is written in the question's language ({language}).

Also give a one-sentence `reason`.
