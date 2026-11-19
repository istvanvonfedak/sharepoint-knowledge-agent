You are grading an answer given by a question-answering assistant against a reference answer.

Question:
<question>
{question}
</question>

Reference answer (correct):
<reference>
{reference}
</reference>

Assistant's answer:
<answer>
{answer}
</answer>

Decide whether the assistant's answer is correct: it gives the same answer as the reference, in substance.
- Numbers count as equal if they agree after rounding to the precision of the reference, or differ by less than
  1% relative. Units and scale must agree once converted ("$1.6 billion" equals "$1,577 million" only if the
  reference's precision allows it; "1,577" with no unit, where the question fixes the unit, is fine).
- Extra detail is fine unless it contradicts the reference. Hedging is fine if the answer still commits to the
  correct value.
- An answer that says it could not find the information is incorrect.
- For yes/no or judgement questions, the conclusion must match the reference; the reasoning may differ.

Return `correct` (true/false) and a one-sentence `reason`.
