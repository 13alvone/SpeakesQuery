# Contributing to SpeakesQuery

Thank you for your interest in SpeakesQuery. Please read this document carefully before contributing - it describes a development model that is intentionally different from most open-source projects.

---

## How This Project Is Built

SpeakesQuery is developed through a human-AI partnership between the project author and Claude (Anthropic). Every feature, fix, and architectural decision goes through a deliberate process: the author defines the vision, consults with Claude on design and implementation, documents the interaction, and makes the final call. This process is not incidental - it is the development methodology, and it produces software with a coherent vision, consistent quality, and clear accountability.

This means **external code contributions are not merged directly into the project.** What is welcomed - and genuinely valued - is something better: your ideas.

---

## What We Want From You: Ideas

The best contributions to SpeakesQuery are **well-explained ideas written in plain language.** You do not need to write code to make a meaningful contribution. You need to think clearly and communicate well.

### Language

Submit ideas in whatever language you communicate best - English, Spanish, Japanese, or any other language. Clarity matters more than language choice. If your idea is strong and well-articulated, it will be understood.

### What Makes a Great Idea Submission

Open a GitHub Issue or Pull Request containing:

1. **The problem or opportunity** - What gap, friction, or missing capability does this address? Who benefits and in what situation?
2. **The proposed solution** - Describe what the feature or change would do, how a user would interact with it, and what the expected behavior looks like. Be specific. "It would be cool if..." is not specific. "When a saved search returns more than N results, the user should be able to configure a threshold that suppresses the alert" is specific.
3. **Why it belongs in SpeakesQuery** - How does this fit the project's philosophy? Does it make the tool more useful for real-world data work without adding unnecessary complexity?
4. **Edge cases and considerations** - What could go wrong? What are the trade-offs? What should this feature explicitly *not* do?

### Optional: Supporting Code and Tests

You are welcome to include code snippets, pseudocode, or test cases that illustrate your idea. Detailed examples and test logic are genuinely appreciated - they demonstrate that you have thought through the mechanics, not just the concept.

However, please understand the following clearly:

> **Contributed code will not be copied into the project.** If your idea is approved, the author and Claude will implement it independently through the project's standard development process. If the resulting implementation happens to resemble your contributed code - because well-reasoned people solving the same problem often arrive at similar solutions - that is incidental, not derivative. Your contribution is the *idea and its articulation*, not the code.

This policy exists to protect the architectural coherence of the project, maintain a single accountable development process, and ensure that every line of code in SpeakesQuery has been reviewed and validated through the same human-AI methodology that built the rest of the system.

---

## How Ideas Are Evaluated

Every submission goes through the same process:

1. **The author reviews the idea** and consults with Claude on feasibility, design implications, and fit within the project's direction.
2. **The consultation is documented** - these interactions form part of the project's decision history.
3. **A go/no-go decision is made.** Not every good idea belongs in every project. A "no" is not a judgment on the quality of your thinking - it means the idea does not fit the current trajectory.
4. **If approved**, the author and Claude iteratively design, implement, and comprehensively test the feature before it enters production.

This is deliberately slow and deliberate. Speed is not the goal. Getting it right is.

---

## What Is Not Accepted

- **Direct code pull requests intended for merge.** Submit ideas, not implementations. (Code as illustration is fine - code as "please merge this" is not.)
- **Features that introduce artificial complexity**, vendor lock-in, rent-seeking mechanisms, or usage restrictions.
- **Obfuscation or unnecessary abstraction.** If the code cannot be read and understood by the people who depend on it, it does not belong here.
- **Changes that compromise security boundaries** - unsafe file access, directory traversal, implicit code execution, or credential exposure.

---

## Security Concerns

If you discover a security vulnerability, please report it privately rather than opening a public issue. Contact the author directly through the channels listed in the repository.

---

## Attribution

All idea contributions are credited in the changelog and commit history when implemented. The project values attribution - professional credit enables accountability and future work.

By contributing, you agree that your submission is licensed under the same terms as the project (Apache License 2.0).

---

## Development Standards (For Reference)

For context on what the implementation process looks like internally:

- Python 3.12.x is required.
- Existing code style and naming conventions are followed.
- New functionality includes tests (228 automated tests and growing - SPQL + API).
- The SPQL query language follows industry-standard search semantics with SpeakesQuery-specific naming conventions.
- All changes are validated against the full test suite before acceptance.
- Native / C++ components must be justified and documented.
- Commits explain *why*, not just *what*.
