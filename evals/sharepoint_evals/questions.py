"""Retrieval questions. `expected` is the doc that answers it; `persona` decides what the caller may see."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Q:
    question: str
    expected: str
    category: str  # semantic | keyword | cross-lingual | restricted
    persona: str = "alice"


QUESTIONS = [
    # Paraphrased: little lexical overlap with the document text
    Q("How far in advance do I need to ask for time off?", "leave-policy.md", "semantic"),
    Q("Can I get my lunch paid for when I'm travelling for work?", "expenses.md", "semantic"),
    Q("What should I do if I get a suspicious email?", "security-incident.md", "semantic"),
    Q("Who helps me out on my very first day?", "onboarding.md", "semantic"),
    Q("What happens if the engineer on call doesn't respond to a page?", "engineering-oncall.md", "semantic"),
    # Exact identifiers where keyword search should shine
    Q("EXP-2041", "expenses.md", "keyword"),
    Q("VPN error 809", "it-helpdesk.md", "keyword"),
    Q("P1 severity response time", "security-incident.md", "keyword"),
    # Question language differs from document language
    Q("How many vacation days do employees in Portugal get?", "politica-ferias-portugal.md", "cross-lingual"),
    Q("Is there a monthly allowance for working from home?", "guia-teletrabalho.md", "cross-lingual"),
    Q("Como devo reportar um email de phishing?", "security-incident.md", "cross-lingual"),
    # Restricted docs: only retrievable for personas with access
    Q("What is the base salary band for level L5?", "salaries-2025.md", "restricted", persona="hr"),
    Q("Which company are we planning to acquire?", "board-minutes.md", "restricted", persona="exec"),
]
