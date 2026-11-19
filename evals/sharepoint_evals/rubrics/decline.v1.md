An assistant answered a question using an organisation's documents. None of the documents contain the answer.

Question:
<question>
{question}
</question>

Assistant's answer:
<answer>
{answer}
</answer>

Decide whether the assistant declined: it said, in substance, that the documents don't answer the question,
without presenting a specific answer as if it were found. Mentioning related information that the documents do
contain is fine, as long as it is not offered as the answer. A confident or "most likely" answer to the question
is not a decline.

Return `declined` (true/false) and a one-sentence `reason`.
