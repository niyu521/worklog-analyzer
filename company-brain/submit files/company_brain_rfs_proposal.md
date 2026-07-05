RFS-Based Problem Definition and Solution Approach

Y Combinator’s RFS on the “Company Brain” highlights the need to collect and structure fragmented company knowledge so that AI can understand how a company actually operates.

Through our work providing AI training, AI agent implementation, and workflow automation for companies, we have encountered this exact problem. We found that one of the most time-consuming parts of AI implementation is not building the AI system itself, but understanding the client’s existing workflows and defining the requirements.

For example, a company may describe a task simply as “creating an invoice.” However, the actual workflow may involve checking customer information, reviewing past transactions, updating a spreadsheet, creating a document, exporting a PDF, and sending it by email. The task has a name, but the actual sequence of tools, actions, and decisions is rarely recorded as structured data.

As a result, AI implementation teams must rely on lengthy interviews and direct observation to reconstruct workflows one by one.

We believe the first bottleneck in building a Company Brain is therefore not simply storing company knowledge. It is capturing how work is actually performed in a format that AI can understand.

Our approach is to observe work rather than ask employees to explain it.

We collect two main types of activity data: change and activity histories from workplace SaaS tools through APIs, and browser interaction logs such as page navigation and user actions. By combining these events chronologically, we can reconstruct how work moves across different tools and identify recurring patterns.

Repeated patterns can then be grouped into actual business workflows, such as invoice creation, customer follow-up, or monthly reporting.

Based on workflow frequency, time spent, tools used, and repetitiveness, the system can recommend the appropriate level of AI delegation: moving a human-operated task to an AI agent, converting repeated agent work into a reusable Skill, or fully automating a workflow through APIs and system integrations.

Our goal is to automatically generate an operational map of how a company actually works and use that map to identify where AI should take over.

Observe Work. Map Work. Delegate Work.

Global Market and User Perspective

We believe this is a global problem faced both by companies adopting AI and by the AI Forward Deployed Engineers (AI FDEs) and implementation teams supporting them.

As companies adopt AI agents and workflow automation, they first need to understand their own operations and determine which tasks are suitable for AI. However, most companies do not have a clear or continuously updated map of their recurring workflows.

Today, this process often depends on consultants or AI implementation teams conducting interviews, observing employees, and manually documenting workflows.

Our first user group is companies that want to adopt AI directly.

By continuously observing day-to-day work, our product automatically identifies recurring workflows and documents them as an operational map. It can then recommend specific opportunities, such as:

* This workflow can be moved to an AI agent using an available MCP integration.
* This repeated agent workflow should be converted into a reusable Skill.
* This high-frequency workflow could be fully automated through API integrations.

This allows companies to continuously discover AI opportunities without relying entirely on external consultants.

Our second user group is AI FDEs and AI implementation firms.

We have experienced firsthand how much time is spent understanding a client’s operations and defining requirements before implementation can begin. We believe this is a shared bottleneck for AI implementation teams globally.

By deploying our product within a client company, AI implementation teams can use real workflow data to identify recurring processes and AI opportunities, turning requirements discovery from an interview-driven process into a data-driven one.

Another important challenge in the global market is the fragmentation of business software. Not every SaaS product has an API or MCP integration. Many companies rely on regional SaaS products, industry-specific systems, legacy web applications, or internal tools.

For this reason, our system does not rely only on API integrations. We also observe browser interactions, allowing us to capture workflows performed through web-based systems even when no direct integration exists.

Our goal is not simply to make AI consulting more efficient.

We aim to build the infrastructure that allows companies to automatically document how they work and continuously discover which tasks can be delegated to AI. At the same time, for AI FDEs, the product becomes a common tool for rapidly understanding client operations.

Product, Technology, and Business Model Overview

Our product is a SaaS platform that continuously collects digital work events and document changes across enterprise tools, reconstructing how work actually flows through an organization.

We are currently developing a collection and event-matching infrastructure for workplace tools such as Google Docs, Google Sheets, Notion, and AI agents such as Claude Code. The system captures service-specific identifiers, revision histories, edited content, timestamps, and other event metadata, and normalizes them into a common event format.

The collected events are matched using a combination of native identifiers, content similarity, and LLM-based contextual reasoning. Events that belong to the same work context are linked under a shared master identity and organized chronologically.

For example, if information from Notion is used to create a Google Doc and the document is later edited through an AI agent, the system can connect events from three different tools as part of one continuous work context.

To support SaaS products and internal web systems without APIs or MCP integrations, we also plan to combine this event infrastructure with browser activity observation. This allows the system to understand workflows across the tools a company actually uses rather than being limited to a predefined integration ecosystem.

The next layer of the product analyzes accumulated event sequences to detect recurring patterns and group them into business workflows such as invoice creation, customer follow-up, or monthly reporting.

Each workflow can then be analyzed based on execution frequency, time spent, tools used, and repetitiveness. The system recommends one of three levels of AI delegation:

Agentize — Identify workflows currently performed manually in a browser that can be moved to an AI agent using available MCP integrations or AI tools.

Skillize — Detect repeated workflows already being performed through AI agents and recommend converting them into reusable Skills.

Automate — Identify high-frequency, repetitive workflows across multiple tools that could be fully automated through APIs and system integrations.

Because the product observes real company operations, it may process highly sensitive business information, customer data, and internal system activity. A simple browser logging architecture is therefore insufficient.

The product requires a secure collection and processing infrastructure built around data minimization, tenant isolation, access control, encryption, and controlled data retention. A key technical challenge is reconstructing workflows across multiple data sources without unnecessarily retaining sensitive raw information.

We see this combination of secure work observation and cross-tool workflow reconstruction as a core technical barrier to entry.

Our business model is primarily a subscription-based SaaS for companies and AI implementation firms.

Companies use the product to automatically document their workflows and continuously discover opportunities for AI adoption without relying entirely on external consultants.

AI FDEs and AI implementation firms use the product to accelerate client discovery and requirements definition across multiple client organizations.

In the initial stage, when the platform identifies workflows with high automation potential, we will also provide implementation services for AI agents, API integrations, and automation systems.

This creates a model in which the SaaS itself continuously discovers automation opportunities, which can then lead to implementation projects.

In the long term, we aim to move beyond workflow discovery and recommendations by enabling Skills and automation workflows to be generated and deployed directly through the platform.

Observe → Map → Recommend → Implement