You are checking whether an answer is supported by the source text it was based on.

Question:
<question>
{question}
</question>

Answer:
<answer>
{answer}
</answer>

Text of the sources the answer cites (everything the assistant's tools returned for them):
<sources>
{sources}
</sources>

Decide whether every factual claim in the answer is supported by the source text. Arithmetic on figures that
appear in the sources (a ratio, a difference, a sum) counts as supported if it is done correctly. General
knowledge that any reader would accept without a source (what a 10-K is) doesn't need support. A claim that the
sources don't contain, or that contradicts them, makes the answer ungrounded. An answer that only says the
information wasn't found is grounded.

Return `grounded` (true/false), `unsupported` (the unsupported claims, quoted briefly; empty if none) and a
one-sentence `reason`.
