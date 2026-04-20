# Task Runner Agent Architecture
**Version:** 1.0  
**Date:** April 20, 2026  
**Purpose:** Secure, scalable filesystem operation gateway for multi-agent systems

---

## Table of Contents
1. [Overview](#overview)
2. [Architecture Components](#architecture-components)
3. [Filesystem Zones](#filesystem-zones)
4. [Permission Model](#permission-model)
5. [Multi-Agent Workflow](#multi-agent-workflow)
6. [Tool Integration with DeepAgent](#tool-integration-with-deepagent)
7. [System Diagrams](#system-diagrams)
8. [Rules Specification](#rules-specification)
9. [Implementation Guidelines](#implementation-guidelines)
10. [Security Considerations](#security-considerations)
11. [Usage Examples](#usage-examples)

---

## Overview

The **Task Runner Agent** serves as a centralized gateway for all filesystem operations in a multi-agent system. It enforces security policies, manages permissions, and provides a consistent interface for any agent that needs to interact with files or directories.

### Core Principles
- **Single Gateway**: All filesystem operations must go through the Task Runner Agent
- **Permission-Based**: Human-in-the-loop for operations outside safe zones
- **Agent-Agnostic**: Any agent (master or sub-agent) can use this system
- **Audit Trail**: All operations are logged for security and debugging
- **Fail-Safe**: Default deny with explicit allow rules

### Key Benefits
✅ Centralized security policy enforcement  
✅ Prevents accidental data loss or corruption  
✅ Clear separation between safe zones and external filesystem  
✅ User maintains control over sensitive operations  
✅ Scalable to any number of agents  

---

## Architecture Components

### 1. Task Runner Agent (Gateway)
**Role:** Single point of entry for all filesystem operations  
**Responsibilities:**
- Receive filesystem operation requests from any agent
- Analyze and validate requested operations
- Determine which filesystem zone is being accessed
- Apply permission rules based on operation type and location
- Prompt user for permission when required
- Execute approved operations in isolated manner
- Return results or errors to requesting agent
- Log all operations for audit trail

### 2. Master Agent (e.g., DeepAgent)
**Role:** Orchestrates high-level tasks  
**Responsibilities:**
- Receives user requests
- Decomposes complex tasks into subtasks
- Spawns sub-agents as needed
- Delegates filesystem operations to Task Runner Agent
- Aggregates results from sub-agents
- Reports final results to user

### 3. Sub-Agents (e.g., Research Agent, Analysis Agent)
**Role:** Specialized task executors  
**Responsibilities:**
- Execute specific subtasks assigned by master agent
- Delegate ALL filesystem operations to Task Runner Agent
- Process data in memory when possible
- Report results back to master agent
- Never directly access filesystem

### 4. User (Human-in-the-Loop)
**Role:** Final authority for sensitive operations  
**Responsibilities:**
- Review permission requests
- Grant or deny access to protected resources
- Monitor agent activities through audit logs

---

## Filesystem Zones

The system defines three distinct filesystem zones with different permission models:

### Zone 1: Sandbox (`/sandbox`)
**Purpose:** Temporary, isolated workspace for code execution and intermediate storage  
**Use Cases:**
- Running Python scripts to analyze data
- Executing code snippets to test functionality
- Storing large intermediate tool outputs temporarily
- Creating temporary files during multi-step operations
- Testing and experimentation

**Permission Model:**
- ✅ **Auto-allow** all operations (read, write, create, delete, execute)
- No user prompt required
- Automatically cleaned up after task completion (optional)
- Isolated from other zones (cannot access parent directories)

**Example Operations:**
```
✅ /sandbox/analysis_script.py          (create & execute)
✅ /sandbox/temp_data.csv                (create & write)
✅ /sandbox/intermediate_results.json    (read & delete)
```

### Zone 2: Workspace (`/workspace`)
**Purpose:** Agent's designated working directory for project files  
**Use Cases:**
- Storing agent-generated reports
- Saving analysis results
- Creating output files for user
- Organizing project-related data

**Permission Model:**
- ✅ **Auto-allow** read, write, create operations
- ⚠️ **Prompt user** for delete operations (safety measure)
- Cannot execute code (use sandbox for that)

**Example Operations:**
```
✅ /workspace/reports/analysis_report.pdf     (create & write)
✅ /workspace/data/processed_data.csv         (read & modify)
⚠️ /workspace/important_file.txt              (delete → prompt)
```

### Zone 3: External Filesystem (`/` excluding sandbox & workspace)
**Purpose:** User's real filesystem outside agent control  
**Use Cases:**
- Reading user-provided files
- Writing outputs to user-specified locations
- Accessing system resources

**Permission Model:**
- ⚠️ **Always prompt user** for ALL operations
- User must explicitly approve each operation
- Include full context (what file, why needed, which agent)
- Special warnings for destructive operations

**Example Operations:**
```
⚠️ /home/user/documents/data.xlsx            (read → prompt)
⚠️ /home/user/downloads/report.pdf           (write → prompt)
🚫 /home/user/documents/data.xlsx            (delete → prompt with WARNING)
🔒 /etc/passwd                                (any operation → DENY)
```

### Zone 4: System-Critical (Protected)
**Purpose:** Operating system and security-critical directories  
**Paths:** `/etc`, `/sys`, `/proc`, `/dev`, `/boot`, `/root`, `/usr/bin`, `/usr/sbin`

**Permission Model:**
- 🚫 **Always deny** all operations
- No user prompt (automatic denial)
- Log attempt for security monitoring

---

## Permission Model

### Operation Types & Required Permissions

| Operation | Sandbox | Workspace | External | System |
|-----------|---------|-----------|----------|--------|
| **READ** | ✅ Allow | ✅ Allow | ⚠️ Prompt | 🚫 Deny |
| **WRITE** | ✅ Allow | ✅ Allow | ⚠️ Prompt | 🚫 Deny |
| **CREATE** | ✅ Allow | ✅ Allow | ⚠️ Prompt | 🚫 Deny |
| **DELETE** | ✅ Allow | ⚠️ Prompt | ⚠️ Prompt⚠️ | 🚫 Deny |
| **EXECUTE** | ✅ Allow | 🚫 Deny | ⚠️ Prompt🔒 | 🚫 Deny |
| **CHMOD/CHOWN** | ✅ Allow | 🚫 Deny | ⚠️ Prompt🔒 | 🚫 Deny |

Legend:
- ✅ = Auto-allow (no prompt)
- ⚠️ = Prompt user for permission
- ⚠️⚠️ = Prompt with strong warning
- ⚠️🔒 = Prompt with security warning
- 🚫 = Always deny

### Permission Request Format

When prompting the user, include:

```yaml
permission_request:
  requesting_agent: "ResearchAgent-001"
  parent_agent: "DeepAgent-Master"
  task_id: "task_1234"
  task_description: "Analyze quarterly sales data"
  operation: "READ"
  target_paths:
    - "/home/user/documents/sales_q4_2025.xlsx"
  reason: "Need to read sales data for trend analysis"
  risk_level: "LOW"  # LOW, MEDIUM, HIGH, CRITICAL
  alternatives: "Could work with copy in /workspace if you prefer"
```

User Response Options:
- ✅ **Allow** - Grant permission for this operation
- 🚫 **Deny** - Reject the operation
- 📋 **Allow with Copy** - Copy to workspace instead (for external files)
- ⏸️ **Defer** - Ask me again later
- 🛑 **Deny All from Agent** - Block this agent temporarily

---

## Multi-Agent Workflow

### Delegation Chain

```
User Request
    ↓
Master Agent (DeepAgent)
    ↓ spawns
Sub-Agent (ResearchAgent)
    ↓ delegates ALL filesystem ops
Task Runner Agent (Gateway)
    ↓ executes (if permitted)
Filesystem
```

### Complete Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                              USER                                    │
│                                                                      │
│  "Analyze the sales data and create a report"                       │
└────────────────┬────────────────────────────────────────────────────┘
                 │
                 ↓
┌─────────────────────────────────────────────────────────────────────┐
│                       MASTER AGENT (DeepAgent)                       │
│                                                                      │
│  1. Receives user request                                           │
│  2. Plans task breakdown:                                           │
│     - Spawn ResearchAgent to analyze data                           │
│     - Spawn ReportAgent to create document                          │
│  3. Monitors sub-agent progress                                     │
│  4. Aggregates results                                              │
└────────────┬─────────────────────────────┬──────────────────────────┘
             │                             │
             │ spawn                       │ spawn
             ↓                             ↓
┌─────────────────────────┐   ┌────────────────────────────┐
│   ResearchAgent         │   │   ReportAgent              │
│                         │   │                            │
│  Task: Analyze sales    │   │  Task: Create PDF report   │
│  data from Excel file   │   │  from analysis results     │
└──────────┬──────────────┘   └─────────┬──────────────────┘
           │                             │
           │ delegates                   │ delegates
           │ filesystem ops              │ filesystem ops
           ↓                             ↓
┌─────────────────────────────────────────────────────────────────────┐
│                    TASK RUNNER AGENT (Gateway)                       │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │ PHASE 1: Request Analysis                                      │ │
│  │ • Parse operation request                                      │ │
│  │ • Extract: operation_type, target_paths, requesting_agent     │ │
│  │ • Build operation dependency graph                            │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                              ↓                                       │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │ PHASE 2: Path Validation & Zone Detection                     │ │
│  │ • Canonicalize all paths (resolve .., symlinks)               │ │
│  │ • Detect filesystem zone for each path                        │ │
│  │ • Check for path traversal attacks                            │ │
│  │ • Validate path existence (for reads)                         │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                              ↓                                       │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │ PHASE 3: Permission Check                                      │ │
│  │                                                                │ │
│  │  Is path in SYSTEM-CRITICAL zone?                             │ │
│  │      YES → ❌ DENY (log attempt)                               │ │
│  │      NO  → Continue                                            │ │
│  │                                                                │ │
│  │  Is path in SANDBOX zone?                                     │ │
│  │      YES → ✅ AUTO-ALLOW                                       │ │
│  │      NO  → Continue                                            │ │
│  │                                                                │ │
│  │  Is path in WORKSPACE zone?                                   │ │
│  │      YES → Check operation type:                              │ │
│  │            • READ/WRITE/CREATE → ✅ AUTO-ALLOW                 │ │
│  │            • DELETE → ⚠️ PROMPT USER                           │ │
│  │            • EXECUTE/CHMOD → ❌ DENY                           │ │
│  │      NO  → Continue                                            │ │
│  │                                                                │ │
│  │  Is path in EXTERNAL zone?                                    │ │
│  │      YES → ⚠️ PROMPT USER (all operations)                     │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                              ↓                                       │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │ PHASE 4: User Permission Request (if needed)                   │ │
│  │                                                                │ │
│  │  Build permission request with full context                   │ │
│  │  Send to user with:                                           │ │
│  │    • Which agent is requesting                                │ │
│  │    • What operation (read/write/delete)                       │ │
│  │    • Which files/paths                                        │ │
│  │    • Why (task context)                                       │ │
│  │    • Risk level                                               │ │
│  │    • Alternatives (if any)                                    │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                              ↓                                       │
│                    ┌──────────────────┐                             │
│                    │  Wait for User   │                             │
│                    │    Response      │                             │
│                    └────┬────────┬────┘                             │
│                         │        │                                  │
│                   ALLOW │        │ DENY                             │
│                         ↓        ↓                                  │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │ PHASE 5: Execution or Rejection                                │ │
│  │                                                                │ │
│  │  If ALLOWED:                                                   │ │
│  │    • Execute operation in isolated environment                │ │
│  │    • Capture output/errors                                    │ │
│  │    • Log operation to audit trail                             │ │
│  │    • Return success result                                    │ │
│  │                                                                │ │
│  │  If DENIED:                                                    │ │
│  │    • Log denial reason                                        │ │
│  │    • Return error with explanation                            │ │
│  │    • Suggest alternatives (copy to workspace, etc.)           │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                              ↓                                       │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │ PHASE 6: Response to Requesting Agent                          │ │
│  │                                                                │ │
│  │  Return structured response:                                  │ │
│  │    • status: "success" | "denied" | "error"                   │ │
│  │    • result: operation output (if success)                    │ │
│  │    • error: error message (if failed)                         │ │
│  │    • alternatives: suggested workarounds (if denied)          │ │
│  │    • operation_id: for audit trail reference                  │ │
│  └────────────────────────────────────────────────────────────────┘ │
└──────────┬───────────────────────────────┬───────────────────────────┘
           │                               │
           │ result                        │ result
           ↓                               ↓
┌─────────────────────────┐   ┌────────────────────────────┐
│   ResearchAgent         │   │   ReportAgent              │
│                         │   │                            │
│  • Receives analysis    │   │  • Receives write result   │
│    data                 │   │  • Confirms report created │
│  • Processes results    │   │                            │
│  • Reports to master    │   │  • Reports to master       │
└──────────┬──────────────┘   └─────────┬──────────────────┘
           │                             │
           │ completion                  │ completion
           └─────────────┬───────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────────────┐
│                       MASTER AGENT (DeepAgent)                       │
│                                                                      │
│  • Receives completion from both sub-agents                         │
│  • Aggregates results                                               │
│  • Prepares final response                                          │
└────────────────────────┬────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────────────┐
│                              USER                                    │
│                                                                      │
│  "Analysis complete! Report saved to /workspace/reports/sales.pdf"  │
└─────────────────────────────────────────────────────────────────────┘
```

### Sequence Diagram (Alternative View)

```
User          MasterAgent    SubAgent    TaskRunnerAgent    Filesystem
 │                │              │               │               │
 │ Request        │              │               │               │
 │───────────────>│              │               │               │
 │                │              │               │               │
 │                │ Spawn        │               │               │
 │                │─────────────>│               │               │
 │                │              │               │               │
 │                │              │ Read Request  │               │
 │                │              │──────────────>│               │
 │                │              │               │               │
 │                │              │               │ Validate Path │
 │                │              │               │──────────┐    │
 │                │              │               │          │    │
 │                │              │               │<─────────┘    │
 │                │              │               │               │
 │                │              │               │ Check Zone   │
 │                │              │               │──────────┐    │
 │                │              │               │          │    │
 │                │              │               │<─────────┘    │
 │                │              │               │               │
 │                │              │  ┌──────────────────────┐     │
 │                │              │  │ Zone = EXTERNAL      │     │
 │                │              │  │ Need User Permission │     │
 │                │              │  └──────────────────────┘     │
 │                │              │               │               │
 │<───────────────────────────────── Permission Request         │
 │                │              │               │               │
 │ "Allow read    │              │               │               │
 │  /home/user/   │              │               │               │
 │  data.xlsx?"   │              │               │               │
 │                │              │               │               │
 │ [User Approves]│              │               │               │
 │────────────────────────────────>              │               │
 │                │              │               │               │
 │                │              │               │ Execute Read  │
 │                │              │               │──────────────>│
 │                │              │               │               │
 │                │              │               │<──────────────│
 │                │              │               │ Data          │
 │                │              │               │               │
 │                │              │<──────────────│               │
 │                │              │  Result       │               │
 │                │              │               │               │
 │                │              │ Process Data  │               │
 │                │              │──────────┐    │               │
 │                │              │          │    │               │
 │                │              │<─────────┘    │               │
 │                │              │               │               │
 │                │              │ Write Request │               │
 │                │              │ (/workspace)  │               │
 │                │              │──────────────>│               │
 │                │              │               │               │
 │                │              │               │ Check Zone   │
 │                │              │               │──────────┐    │
 │                │              │               │          │    │
 │                │              │               │<─────────┘    │
 │                │              │               │               │
 │                │              │  ┌──────────────────────┐     │
 │                │              │  │ Zone = WORKSPACE     │     │
 │                │              │  │ Auto-Allow Write     │     │
 │                │              │  └──────────────────────┘     │
 │                │              │               │               │
 │                │              │               │ Execute Write │
 │                │              │               │──────────────>│
 │                │              │               │               │
 │                │              │               │<──────────────│
 │                │              │               │ Success       │
 │                │              │               │               │
 │                │              │<──────────────│               │
 │                │              │  Result       │               │
 │                │              │               │               │
 │                │<─────────────│               │               │
 │                │  Complete    │               │               │
 │                │              │               │               │
 │<───────────────│              │               │               │
 │  Final Result  │              │               │               │
 │                │              │               │               │
```

---

## Tool Integration with DeepAgent

### Overview: Preserving Tool Calling Capabilities

A critical concern when introducing the Task Runner Agent is whether it interferes with the DeepAgent's existing tool calling capabilities. **The answer is NO** - the Task Runner Agent **enhances** rather than replaces the existing tool infrastructure.

The Task Runner Agent integrates as:
- **A set of tools** that the DeepAgent calls (alongside existing tools)
- **A transparent middleware layer** that intercepts only filesystem operations
- **An additive component** that preserves all existing capabilities

### Integration Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                         DEEPAGENT                               │
│                    (LangChain/LangGraph)                        │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                     TOOL REGISTRY                         │  │
│  │                                                           │  │
│  │  Filesystem Tools (Task Runner):                         │  │
│  │    • read_file()                                         │  │
│  │    • write_file()                                        │  │
│  │    • delete_file()                                       │  │
│  │    • execute_code()                                      │  │
│  │         ↓ (delegate to Task Runner Agent)                │  │
│  │                                                           │  │
│  │  Other Tools (unchanged):                                │  │
│  │    • web_search()                                        │  │
│  │    • send_email()                                        │  │
│  │    • query_database()                                    │  │
│  │    • call_api()                                          │  │
│  │    • generate_image()                                    │  │
│  │         ↓ (execute normally)                             │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  Agent can freely call ANY tool and chain them together        │
└────────────────────────────────────────────────────────────────┘
```

### Method 1: Explicit Tool Wrapper (Recommended)

Define Task Runner operations as standard LangChain tools:

```python
# task_runner_tools.py
from langchain_core.tools import tool
from typing import Optional
import uuid

# Initialize Task Runner Agent (singleton)
task_runner = TaskRunnerAgent()

@tool
def read_file(
    path: str,
    reason: str,
    task_id: Optional[str] = None
) -> dict:
    """
    Read a file through the Task Runner Agent security gateway.
    
    This tool handles:
    - Permission checking based on file location
    - User prompts for external files
    - Path validation and security
    - Audit logging
    
    Args:
        path: Absolute path to the file to read
        reason: Why you need to read this file (shown to user in prompts)
        task_id: Optional task identifier for tracking
    
    Returns:
        dict with 'status', 'content' (if success), or 'error'/'alternatives' (if denied)
    
    Examples:
        read_file("/workspace/data.csv", "Load data for analysis")
        read_file("/home/user/report.pdf", "Extract report findings", task_id="research_001")
    """
    from langgraph.graph import get_current_thread
    
    # Get current agent context
    thread = get_current_thread()
    agent_id = thread.values.get("agent_id", "deepagent-master")
    parent_id = thread.values.get("parent_agent_id")
    
    # Create operation request
    request = OperationRequest(
        request_id=str(uuid.uuid4()),
        requesting_agent=agent_id,
        parent_agent=parent_id,
        task_id=task_id or str(uuid.uuid4()),
        task_description=reason,
        operation=OperationType.READ,
        paths=[path],
        reason=reason
    )
    
    # Execute through Task Runner
    response = task_runner.execute_operation(request)
    
    # Return formatted result
    if response.status == "success":
        return {
            "status": "success",
            "content": response.result,
            "operation_id": response.operation_id
        }
    else:
        return {
            "status": "denied" if response.status == "denied" else "error",
            "error": response.error,
            "alternatives": response.alternatives
        }

@tool
def write_file(
    path: str,
    content: str,
    reason: str,
    task_id: Optional[str] = None
) -> dict:
    """
    Write content to a file through the Task Runner Agent security gateway.
    
    Args:
        path: Absolute path where to write the file
        content: Content to write
        reason: Why you're writing this file
        task_id: Optional task identifier
    
    Returns:
        dict with 'status' and 'operation_id' or 'error'
    """
    from langgraph.graph import get_current_thread
    
    thread = get_current_thread()
    agent_id = thread.values.get("agent_id", "deepagent-master")
    parent_id = thread.values.get("parent_agent_id")
    
    request = OperationRequest(
        request_id=str(uuid.uuid4()),
        requesting_agent=agent_id,
        parent_agent=parent_id,
        task_id=task_id or str(uuid.uuid4()),
        task_description=reason,
        operation=OperationType.WRITE,
        paths=[path],
        reason=reason,
        additional_context={"content": content}
    )
    
    response = task_runner.execute_operation(request)
    
    if response.status == "success":
        return {
            "status": "success",
            "path": path,
            "operation_id": response.operation_id
        }
    else:
        return {
            "status": response.status,
            "error": response.error,
            "alternatives": response.alternatives
        }

@tool
def execute_code(
    script_path: str,
    reason: str,
    command: Optional[str] = None,
    timeout: int = 300,
    task_id: Optional[str] = None
) -> dict:
    """
    Execute code in the sandbox through the Task Runner Agent.
    
    Args:
        script_path: Path to script (should be in /sandbox)
        reason: Why you're executing this code
        command: Full command to execute (default: python script_path)
        timeout: Maximum execution time in seconds
        task_id: Optional task identifier
    
    Returns:
        dict with 'status', 'output', 'exit_code' or 'error'
    """
    from langgraph.graph import get_current_thread
    
    thread = get_current_thread()
    agent_id = thread.values.get("agent_id", "deepagent-master")
    parent_id = thread.values.get("parent_agent_id")
    
    request = OperationRequest(
        request_id=str(uuid.uuid4()),
        requesting_agent=agent_id,
        parent_agent=parent_id,
        task_id=task_id or str(uuid.uuid4()),
        task_description=reason,
        operation=OperationType.EXECUTE,
        paths=[script_path],
        reason=reason,
        additional_context={
            "command": command or f"python {script_path}",
            "timeout": timeout
        }
    )
    
    response = task_runner.execute_operation(request)
    
    if response.status == "success":
        return {
            "status": "success",
            "output": response.result.get("output"),
            "exit_code": response.result.get("exit_code"),
            "operation_id": response.operation_id
        }
    else:
        return {
            "status": response.status,
            "error": response.error,
            "alternatives": response.alternatives
        }

@tool
def delete_file(
    path: str,
    reason: str,
    task_id: Optional[str] = None
) -> dict:
    """
    Delete a file through the Task Runner Agent (requires confirmation for important files).
    
    Args:
        path: Absolute path to file to delete
        reason: Why you're deleting this file
        task_id: Optional task identifier
    
    Returns:
        dict with 'status' or 'error'
    """
    from langgraph.graph import get_current_thread
    
    thread = get_current_thread()
    agent_id = thread.values.get("agent_id", "deepagent-master")
    parent_id = thread.values.get("parent_agent_id")
    
    request = OperationRequest(
        request_id=str(uuid.uuid4()),
        requesting_agent=agent_id,
        parent_agent=parent_id,
        task_id=task_id or str(uuid.uuid4()),
        task_description=reason,
        operation=OperationType.DELETE,
        paths=[path],
        reason=reason
    )
    
    response = task_runner.execute_operation(request)
    
    if response.status == "success":
        return {
            "status": "success",
            "deleted": path,
            "operation_id": response.operation_id
        }
    else:
        return {
            "status": response.status,
            "error": response.error,
            "alternatives": response.alternatives
        }
```

### Creating the DeepAgent with All Tools

```python
# deepagent.py
from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic
from task_runner_tools import read_file, write_file, execute_code, delete_file

# Import other existing tools
from tools.web_tools import web_search, web_scrape
from tools.email_tools import send_email, read_email
from tools.database_tools import query_database
from tools.api_tools import call_rest_api

# Initialize LLM
llm = ChatAnthropic(model="claude-sonnet-4-20250514")

# Combine ALL tools (Task Runner + Others)
all_tools = [
    # Task Runner filesystem tools
    read_file,
    write_file,
    execute_code,
    delete_file,
    
    # Other tools (unchanged, work normally)
    web_search,
    web_scrape,
    send_email,
    read_email,
    query_database,
    call_rest_api,
]

# Create DeepAgent with full tool set
deepagent = create_react_agent(
    model=llm,
    tools=all_tools,
    state_modifier="""You are DeepAgent, a helpful AI assistant with access to:
    
    Filesystem operations (via Task Runner Agent):
    - read_file: Read files with security checks
    - write_file: Write files with permission management
    - execute_code: Run code in isolated sandbox
    - delete_file: Delete files with confirmations
    
    Other capabilities:
    - Web search and scraping
    - Email management
    - Database queries
    - API calls
    
    When working with files:
    - Use /sandbox for temporary code execution
    - Use /workspace for your working files
    - Always provide clear reasons when accessing user files
    - Handle permission denials gracefully by suggesting alternatives
    """
)

# Example usage
def run_deepagent(user_request: str):
    """Run DeepAgent with user request"""
    
    result = deepagent.invoke({
        "messages": [{"role": "user", "content": user_request}]
    })
    
    return result
```

### Usage Example: Multi-Tool Workflow

```python
# Example: DeepAgent uses BOTH filesystem and web tools together

user_request = """
Search the web for Python data analysis tutorials.
Download the best tutorial PDF.
Create a summary script that extracts key concepts.
Run the script and save results to my workspace.
"""

# DeepAgent's internal reasoning and tool calls:

# Step 1: Web search (normal tool, NOT through Task Runner)
search_results = agent.call_tool(
    "web_search",
    query="best Python data analysis tutorials 2026 PDF"
)

# Step 2: Download PDF (write_file tool → goes through Task Runner)
tutorial_url = search_results[0]["url"]
pdf_content = download_pdf(tutorial_url)

download_result = agent.call_tool(
    "write_file",
    path="/workspace/tutorials/python_analysis_tutorial.pdf",
    content=pdf_content,
    reason="Save downloaded tutorial for analysis",
    task_id="tutorial_analysis_001"
)
# Task Runner: workspace write → auto-allowed ✅

# Step 3: Create analysis script (write_file to sandbox)
script_content = """
import PyPDF2
import json

# Read tutorial PDF
with open('/workspace/tutorials/python_analysis_tutorial.pdf', 'rb') as f:
    pdf = PyPDF2.PdfReader(f)
    text = ' '.join([page.extract_text() for page in pdf.pages])

# Extract key concepts (simplified)
concepts = extract_concepts(text)

# Save to workspace
with open('/workspace/tutorials/key_concepts.json', 'w') as f:
    json.dump(concepts, f)

print(f"Extracted {len(concepts)} key concepts")
"""

script_result = agent.call_tool(
    "write_file",
    path="/sandbox/analyze_tutorial.py",
    content=script_content,
    reason="Create script to analyze tutorial PDF",
    task_id="tutorial_analysis_001"
)
# Task Runner: sandbox write → auto-allowed ✅

# Step 4: Execute script (execute_code tool → goes through Task Runner)
exec_result = agent.call_tool(
    "execute_code",
    script_path="/sandbox/analyze_tutorial.py",
    reason="Extract key concepts from tutorial",
    task_id="tutorial_analysis_001"
)
# Task Runner: sandbox execution → auto-allowed ✅
# Script reads from workspace → auto-allowed ✅
# Script writes to workspace → auto-allowed ✅

# All tools work seamlessly together!
```

### Method 2: Automatic Interception (Advanced)

For more transparent integration, you can intercept filesystem operations automatically:

```python
# filesystem_interceptor.py
from langchain_core.tools import BaseTool
from typing import Any, Optional
import os

class FileSystemToolInterceptor(BaseTool):
    """
    Wrapper that automatically intercepts filesystem operations
    and routes them through Task Runner Agent.
    """
    
    def __init__(
        self, 
        original_tool: BaseTool,
        task_runner: TaskRunnerAgent,
        agent_context: dict
    ):
        self.original_tool = original_tool
        self.task_runner = task_runner
        self.agent_context = agent_context
        super().__init__(
            name=original_tool.name,
            description=original_tool.description
        )
    
    def _run(self, *args, **kwargs) -> Any:
        """
        Intercept tool call and route filesystem ops through Task Runner
        """
        # Detect if this tool touches the filesystem
        if self._is_filesystem_operation(args, kwargs):
            return self._route_through_task_runner(args, kwargs)
        else:
            # Execute normally
            return self.original_tool._run(*args, **kwargs)
    
    def _is_filesystem_operation(self, args, kwargs) -> bool:
        """
        Detect if tool will perform filesystem operations
        """
        # Check for file paths in arguments
        for arg in list(args) + list(kwargs.values()):
            if isinstance(arg, str):
                # Check if it looks like a file path
                if (arg.startswith('/') or 
                    os.path.isabs(arg) or 
                    '.' in os.path.basename(arg)):
                    return True
        
        # Check tool name/description for filesystem keywords
        fs_keywords = ['file', 'read', 'write', 'delete', 'save', 'load']
        tool_text = f"{self.original_tool.name} {self.original_tool.description}".lower()
        
        return any(keyword in tool_text for keyword in fs_keywords)
    
    def _route_through_task_runner(self, args, kwargs) -> Any:
        """
        Route the operation through Task Runner Agent
        """
        # Extract operation details
        operation_type = self._infer_operation_type()
        paths = self._extract_paths(args, kwargs)
        
        # Create request
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=self.agent_context.get("agent_id"),
            parent_agent=self.agent_context.get("parent_agent_id"),
            task_id=self.agent_context.get("task_id"),
            task_description=f"Tool: {self.original_tool.name}",
            operation=operation_type,
            paths=paths,
            reason=self.original_tool.description
        )
        
        # Execute through Task Runner
        response = self.task_runner.execute_operation(request)
        
        if response.status == "success":
            # If allowed, execute original tool
            return self.original_tool._run(*args, **kwargs)
        else:
            # Return error
            raise PermissionError(
                f"Task Runner denied operation: {response.error}\n"
                f"Alternatives: {response.alternatives}"
            )
    
    def _infer_operation_type(self) -> OperationType:
        """Infer operation type from tool name/description"""
        name_lower = self.original_tool.name.lower()
        
        if 'read' in name_lower or 'load' in name_lower:
            return OperationType.READ
        elif 'write' in name_lower or 'save' in name_lower:
            return OperationType.WRITE
        elif 'delete' in name_lower or 'remove' in name_lower:
            return OperationType.DELETE
        elif 'execute' in name_lower or 'run' in name_lower:
            return OperationType.EXECUTE
        else:
            return OperationType.READ  # Default
    
    def _extract_paths(self, args, kwargs) -> list:
        """Extract file paths from arguments"""
        paths = []
        
        for arg in list(args) + list(kwargs.values()):
            if isinstance(arg, str) and (arg.startswith('/') or os.path.isabs(arg)):
                paths.append(arg)
        
        return paths

# Usage: Wrap all tools automatically
def wrap_tools_with_task_runner(
    tools: list[BaseTool],
    task_runner: TaskRunnerAgent,
    agent_context: dict
) -> list[BaseTool]:
    """
    Automatically wrap all tools with Task Runner interception
    """
    wrapped = []
    
    for tool in tools:
        wrapped_tool = FileSystemToolInterceptor(
            original_tool=tool,
            task_runner=task_runner,
            agent_context=agent_context
        )
        wrapped.append(wrapped_tool)
    
    return wrapped

# Create agent with auto-wrapped tools
task_runner = TaskRunnerAgent()
original_tools = [web_search, send_email, save_file, load_file, ...]

wrapped_tools = wrap_tools_with_task_runner(
    tools=original_tools,
    task_runner=task_runner,
    agent_context={"agent_id": "deepagent-master"}
)

deepagent = create_react_agent(llm, tools=wrapped_tools)
```

### Sub-Agent Tool Inheritance

When DeepAgent spawns sub-agents, they inherit the same tool set:

```python
# deepagent.py
class DeepAgent:
    def __init__(self, tools):
        self.tools = tools  # Includes Task Runner tools
        self.agent_id = "deepagent-master"
    
    def spawn_research_agent(self, task_description: str):
        """
        Spawn a ResearchAgent sub-agent with same tools
        """
        from langgraph.prebuilt import create_react_agent
        
        # Sub-agent gets SAME tool set
        research_agent = create_react_agent(
            model=llm,
            tools=self.tools,  # Inherits all tools including Task Runner
            state_modifier=f"""You are a Research Agent working on: {task_description}
            
            You have access to the same tools as your parent agent.
            When using filesystem operations, they will be tracked under your agent ID.
            Your parent agent: {self.agent_id}
            """
        )
        
        # Set agent context for Task Runner
        research_agent_id = f"research-agent-{uuid.uuid4().hex[:8]}"
        
        # Update agent context in thread state
        def run_with_context(user_input):
            return research_agent.invoke({
                "messages": [{"role": "user", "content": user_input}],
                "agent_id": research_agent_id,
                "parent_agent_id": self.agent_id,
                "task_id": f"task_{uuid.uuid4().hex[:8]}"
            })
        
        return run_with_context

# Example usage
deepagent = DeepAgent(tools=all_tools)

# User asks for research
research_task = "Analyze sales data from /home/user/sales_2025.xlsx"

# DeepAgent spawns research agent
research_agent_run = deepagent.spawn_research_agent(research_task)

# Research agent uses tools (including Task Runner tools)
result = research_agent_run(research_task)

# When research agent calls read_file("/home/user/sales_2025.xlsx", ...):
# - Task Runner sees requesting_agent: "research-agent-abc123"
# - Task Runner sees parent_agent: "deepagent-master"
# - User prompt shows full hierarchy
# - Permission granted/denied applies to this specific agent instance
```

### Key Benefits of This Integration

✅ **No Capability Loss**: DeepAgent retains ALL existing tools  
✅ **Selective Enforcement**: Only filesystem operations are intercepted  
✅ **Transparent to Agent**: From agent's perspective, just calling tools  
✅ **Tool Composition**: Can chain filesystem tools with other tools  
✅ **Hierarchical Context**: Sub-agents properly tracked in permission system  
✅ **Flexible Integration**: Choose explicit wrapper or automatic interception  
✅ **LangChain Native**: Works seamlessly with LangChain/LangGraph patterns  

### Integration Checklist

When integrating Task Runner Agent with your DeepAgent:

- [ ] Define filesystem operation tools (read, write, execute, delete)
- [ ] Initialize Task Runner Agent singleton
- [ ] Add Task Runner tools to DeepAgent's tool registry
- [ ] Update agent state to include agent_id and parent_agent_id
- [ ] Configure tool inheritance for sub-agents
- [ ] Test permission flows for each filesystem zone
- [ ] Verify other tools (web search, email, etc.) work unchanged
- [ ] Implement error handling for permission denials
- [ ] Add audit logging integration
- [ ] Document tool usage in agent prompts

---

## System Diagrams

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         AGENT ECOSYSTEM                              │
│                                                                      │
│  ┌────────────────┐         ┌────────────────┐                     │
│  │ Master Agent   │────────>│  Sub-Agent 1   │                     │
│  │  (DeepAgent)   │         │ (ResearchAgent)│                     │
│  └────────────────┘         └────────────────┘                     │
│         │                            │                              │
│         │                            │                              │
│         │                   ┌────────────────┐                     │
│         │                   │  Sub-Agent 2   │                     │
│         └──────────────────>│ (AnalysisAgent)│                     │
│                             └────────────────┘                     │
│                                      │                              │
│         ALL agents delegate          │                              │
│         filesystem operations        │                              │
│         to single gateway            │                              │
│                └─────────────────────┘                              │
│                          │                                          │
│                          ↓                                          │
│         ┌────────────────────────────────────┐                     │
│         │    TASK RUNNER AGENT (Gateway)     │                     │
│         │                                    │                     │
│         │  • Permission enforcement          │                     │
│         │  • Path validation                 │                     │
│         │  • Zone detection                  │                     │
│         │  • User prompting (HITL)           │                     │
│         │  • Audit logging                   │                     │
│         │  • Operation execution             │                     │
│         └────────────────────────────────────┘                     │
│                          │                                          │
└──────────────────────────┼──────────────────────────────────────────┘
                           │
                           ↓
         ┌─────────────────────────────────────┐
         │         FILESYSTEM                  │
         │                                     │
         │  ┌──────────┐  ┌──────────┐        │
         │  │ /sandbox │  │/workspace│        │
         │  │          │  │          │        │
         │  │ Auto-    │  │ Auto-    │        │
         │  │ Allow    │  │ Allow*   │        │
         │  └──────────┘  └──────────┘        │
         │                                     │
         │  ┌─────────────────────────┐       │
         │  │ External Filesystem     │       │
         │  │ /home/user/...          │       │
         │  │                         │       │
         │  │ Prompt Required         │       │
         │  └─────────────────────────┘       │
         │                                     │
         │  ┌─────────────────────────┐       │
         │  │ System Critical         │       │
         │  │ /etc, /sys, /proc...    │       │
         │  │                         │       │
         │  │ Always Deny             │       │
         │  └─────────────────────────┘       │
         └─────────────────────────────────────┘
```

### Decision Flow for Task Runner Agent

```
                        ┌─────────────────────┐
                        │  Receive Operation  │
                        │     Request         │
                        └──────────┬──────────┘
                                   │
                                   ↓
                        ┌─────────────────────┐
                        │ Canonicalize Path   │
                        │ (resolve .., links) │
                        └──────────┬──────────┘
                                   │
                                   ↓
                        ┌─────────────────────┐
                        │ Check Path Traversal│
                        │   Attack?           │
                        └──────────┬──────────┘
                                   │
                        YES ┌──────┴──────┐ NO
                            │             │
                            ↓             ↓
                   ┌─────────────┐  ┌─────────────────┐
                   │ DENY & LOG  │  │ Detect Zone     │
                   │ (Security)  │  │                 │
                   └─────────────┘  └────────┬────────┘
                                             │
                      ┌──────────────────────┼──────────────────────┐
                      │                      │                      │
                      ↓                      ↓                      ↓
            ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
            │ SYSTEM-CRITICAL? │  │    SANDBOX?      │  │   WORKSPACE?     │
            │ /etc, /sys, etc. │  │   /sandbox       │  │  /workspace      │
            └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
                     │                     │                     │
                    YES                   YES                   YES
                     │                     │                     │
                     ↓                     ↓                     ↓
            ┌─────────────────┐  ┌─────────────────┐  ┌──────────────────┐
            │  DENY (Always)  │  │  ALLOW (All ops)│  │  Check Operation │
            │  Log Attempt    │  │  Execute        │  │      Type        │
            └─────────────────┘  └─────────────────┘  └────────┬─────────┘
                                                                │
                                          ┌─────────────────────┼───────────────┐
                                          │                     │               │
                                          ↓                     ↓               ↓
                                    ┌──────────┐          ┌─────────┐    ┌──────────┐
                                    │READ/WRITE│          │ DELETE? │    │EXECUTE/  │
                                    │ CREATE?  │          │         │    │ CHMOD?   │
                                    └────┬─────┘          └────┬────┘    └────┬─────┘
                                         │                     │              │
                                        YES                   YES            YES
                                         │                     │              │
                                         ↓                     ↓              ↓
                                    ┌─────────┐          ┌──────────┐   ┌─────────┐
                                    │ ALLOW   │          │ PROMPT   │   │  DENY   │
                                    │ Execute │          │   USER   │   │         │
                                    └─────────┘          └──────────┘   └─────────┘
                                                                  │
                      NO from all zones above ─────────────────────┘
                                          │
                                          ↓
                                ┌──────────────────┐
                                │  EXTERNAL ZONE   │
                                │ (Everything else)│
                                └────────┬─────────┘
                                         │
                                         ↓
                                ┌──────────────────┐
                                │  PROMPT USER     │
                                │  (All operations)│
                                └────────┬─────────┘
                                         │
                              ┌──────────┴──────────┐
                              │                     │
                           ALLOW                  DENY
                              │                     │
                              ↓                     ↓
                     ┌─────────────────┐   ┌─────────────────┐
                     │ Execute & Log   │   │ Return Error    │
                     │ Return Result   │   │ Suggest Alt.    │
                     └─────────────────┘   └─────────────────┘
```

---

## Rules Specification

### Complete Rule Set (YAML Format)

```yaml
# Task Runner Agent - Permission Rules
version: "1.0"
last_updated: "2026-04-20"

# Filesystem zone definitions
zones:
  sandbox:
    path: "/sandbox"
    description: "Temporary workspace for code execution and intermediate storage"
    auto_cleanup: true
    isolation: true
    
  workspace:
    path: "/workspace"
    description: "Agent's designated working directory"
    persistent: true
    
  external:
    description: "User's filesystem outside agent control"
    paths: 
      - "/*"
    exclude:
      - "/sandbox"
      - "/workspace"
      - system_critical
    
  system_critical:
    description: "Operating system and security-critical directories"
    paths:
      - "/etc"
      - "/sys"
      - "/proc"
      - "/dev"
      - "/boot"
      - "/root"
      - "/usr/bin"
      - "/usr/sbin"
      - "/var/log"

# Permission rules by zone and operation
rules:
  
  # SYSTEM-CRITICAL ZONE - Always Deny
  - zone: system_critical
    operations: [READ, WRITE, CREATE, DELETE, EXECUTE, CHMOD, CHOWN]
    action: DENY
    reason: "System directories are protected"
    log_level: CRITICAL
    
  # SANDBOX ZONE - Auto-allow all operations
  - zone: sandbox
    operations: [READ, WRITE, CREATE, DELETE, EXECUTE]
    action: ALLOW
    reason: "Sandbox is isolated temporary workspace"
    log_level: INFO
    
  - zone: sandbox
    operations: [CHMOD, CHOWN]
    action: ALLOW
    reason: "Permission changes allowed within sandbox"
    log_level: INFO
    
  # WORKSPACE ZONE - Mixed permissions
  - zone: workspace
    operations: [READ, WRITE, CREATE]
    action: ALLOW
    reason: "Standard workspace operations"
    log_level: INFO
    
  - zone: workspace
    operations: [DELETE]
    action: PROMPT
    prompt_config:
      message: "Agent '{agent}' wants to DELETE files in workspace"
      warning: "This action cannot be undone"
      risk_level: MEDIUM
      show_paths: true
      show_alternatives: false
    log_level: WARNING
    
  - zone: workspace
    operations: [EXECUTE, CHMOD, CHOWN]
    action: DENY
    reason: "Code execution should use sandbox; permission changes not allowed in workspace"
    alternatives:
      - "For code execution, copy to /sandbox"
      - "For permission changes, request external zone access"
    log_level: WARNING
    
  # EXTERNAL ZONE - Always prompt
  - zone: external
    operations: [READ]
    action: PROMPT
    prompt_config:
      message: "Agent '{agent}' wants to READ external files"
      risk_level: LOW
      show_paths: true
      show_alternatives: true
      alternatives:
        - "Copy to /workspace for safer access"
    log_level: INFO
    
  - zone: external
    operations: [WRITE, CREATE]
    action: PROMPT
    prompt_config:
      message: "Agent '{agent}' wants to WRITE to external filesystem"
      warning: "This will modify files outside agent workspace"
      risk_level: MEDIUM
      show_paths: true
      show_alternatives: true
      alternatives:
        - "Write to /workspace instead"
    log_level: WARNING
    
  - zone: external
    operations: [DELETE]
    action: PROMPT
    prompt_config:
      message: "Agent '{agent}' wants to DELETE external files"
      warning: "⚠️ DESTRUCTIVE OPERATION - This cannot be undone!"
      risk_level: HIGH
      show_paths: true
      show_file_sizes: true
      require_confirmation: true
    log_level: CRITICAL
    
  - zone: external
    operations: [EXECUTE]
    action: PROMPT
    prompt_config:
      message: "Agent '{agent}' wants to EXECUTE code in external filesystem"
      warning: "🔒 SECURITY RISK - Executing external code"
      risk_level: CRITICAL
      show_paths: true
      show_alternatives: true
      alternatives:
        - "Copy to /sandbox for isolated execution"
      require_confirmation: true
    log_level: CRITICAL
    
  - zone: external
    operations: [CHMOD, CHOWN]
    action: PROMPT
    prompt_config:
      message: "Agent '{agent}' wants to change file permissions"
      warning: "🔒 SECURITY RISK - Permission modifications"
      risk_level: CRITICAL
      show_paths: true
      require_confirmation: true
    log_level: CRITICAL

# Security settings
security:
  path_validation:
    canonicalize_paths: true
    resolve_symlinks: true
    block_path_traversal: true
    max_path_length: 4096
    
  rate_limiting:
    enabled: true
    max_requests_per_minute: 100
    max_prompts_per_task: 10
    
  audit_logging:
    enabled: true
    log_all_requests: true
    log_file: "/var/log/task-runner-agent/audit.log"
    retention_days: 90
    
  isolation:
    sandbox_network_access: false
    sandbox_max_execution_time: 300  # 5 minutes
    workspace_max_size_gb: 10

# User prompt templates
prompt_templates:
  standard:
    title: "🤖 Agent Permission Request"
    fields:
      - agent: "Requesting Agent: {agent_name} (ID: {agent_id})"
      - parent: "Parent Agent: {parent_agent}"
      - task: "Task: {task_description}"
      - operation: "Operation: {operation_type}"
      - paths: "Target Paths: {path_list}"
      - reason: "Reason: {reason}"
      - risk: "Risk Level: {risk_level}"
    buttons:
      - "✅ Allow"
      - "🚫 Deny"
      - "📋 Allow with Copy to Workspace"
      - "⏸️ Ask Again Later"
      
  destructive:
    title: "⚠️ DESTRUCTIVE OPERATION WARNING"
    fields:
      - agent: "Requesting Agent: {agent_name}"
      - operation: "⚠️ WILL {operation_type}: {path_list}"
      - warning: "THIS CANNOT BE UNDONE!"
      - file_info: "File Sizes: {total_size}"
      - task: "Reason: {task_description}"
    buttons:
      - "🚫 Deny (Recommended)"
      - "✅ Allow (I understand the risk)"
      - "📋 Copy to Workspace Instead"
```

### Rule Priority

When multiple rules could apply:
1. **System-Critical** (highest priority) - Always checked first
2. **Explicit Denies** - Operations never allowed in any zone
3. **Zone-Specific Rules** - Most specific zone match
4. **Default Deny** - If no rule matches, deny the operation

---

## Implementation Guidelines

### 1. Task Runner Agent Core Interface

```python
from typing import Literal, List, Optional
from enum import Enum
from pydantic import BaseModel

class OperationType(Enum):
    READ = "read"
    WRITE = "write"
    CREATE = "create"
    DELETE = "delete"
    EXECUTE = "execute"
    CHMOD = "chmod"
    CHOWN = "chown"

class FilesystemZone(Enum):
    SANDBOX = "sandbox"
    WORKSPACE = "workspace"
    EXTERNAL = "external"
    SYSTEM_CRITICAL = "system_critical"

class PermissionAction(Enum):
    ALLOW = "allow"
    DENY = "deny"
    PROMPT = "prompt"

class OperationRequest(BaseModel):
    """Request from an agent to perform filesystem operation"""
    request_id: str
    requesting_agent: str
    parent_agent: Optional[str]
    task_id: str
    task_description: str
    operation: OperationType
    paths: List[str]
    reason: str
    additional_context: Optional[dict] = None

class OperationResponse(BaseModel):
    """Response from Task Runner Agent"""
    request_id: str
    status: Literal["success", "denied", "error"]
    result: Optional[any] = None
    error: Optional[str] = None
    alternatives: Optional[List[str]] = None
    operation_id: str  # For audit trail

class TaskRunnerAgent:
    """Core Task Runner Agent interface"""
    
    def execute_operation(self, request: OperationRequest) -> OperationResponse:
        """
        Main entry point for all filesystem operations.
        
        Workflow:
        1. Validate request
        2. Canonicalize paths
        3. Detect zones
        4. Apply rules
        5. Prompt user if needed
        6. Execute or deny
        7. Log and return result
        """
        pass
    
    def _validate_request(self, request: OperationRequest) -> bool:
        """Validate operation request structure and contents"""
        pass
    
    def _canonicalize_path(self, path: str) -> str:
        """Resolve path to canonical form (no .., symlinks resolved)"""
        pass
    
    def _detect_zone(self, canonical_path: str) -> FilesystemZone:
        """Determine which filesystem zone the path belongs to"""
        pass
    
    def _check_path_traversal(self, original: str, canonical: str) -> bool:
        """Detect path traversal attack attempts"""
        pass
    
    def _apply_rules(
        self, 
        zone: FilesystemZone, 
        operation: OperationType
    ) -> PermissionAction:
        """Apply rule set to determine required action"""
        pass
    
    def _prompt_user(
        self, 
        request: OperationRequest, 
        zone: FilesystemZone
    ) -> bool:
        """Prompt user for permission and wait for response"""
        pass
    
    def _execute_allowed_operation(
        self, 
        request: OperationRequest
    ) -> OperationResponse:
        """Execute the filesystem operation in isolated manner"""
        pass
    
    def _log_operation(
        self, 
        request: OperationRequest, 
        response: OperationResponse
    ):
        """Log operation to audit trail"""
        pass
```

### 2. Integration with Existing Agents

```python
# Example: How a sub-agent would use Task Runner Agent

class ResearchAgent:
    def __init__(self, task_runner_agent: TaskRunnerAgent):
        self.task_runner = task_runner_agent
        self.agent_id = "research-agent-001"
    
    def analyze_sales_data(self, data_file_path: str):
        """Analyze sales data from external file"""
        
        # 1. Request READ permission via Task Runner
        read_request = OperationRequest(
            request_id=generate_uuid(),
            requesting_agent=self.agent_id,
            parent_agent="deepagent-master",
            task_id="analyze-sales-q4",
            task_description="Analyze Q4 2025 sales trends",
            operation=OperationType.READ,
            paths=[data_file_path],
            reason="Need to read sales data for trend analysis"
        )
        
        read_response = self.task_runner.execute_operation(read_request)
        
        if read_response.status != "success":
            # Handle denial - maybe suggest alternative
            return {
                "status": "failed",
                "reason": read_response.error,
                "alternatives": read_response.alternatives
            }
        
        # 2. Process data (in memory)
        data = read_response.result
        analysis_results = self._perform_analysis(data)
        
        # 3. Request WRITE permission to save results (to workspace)
        write_request = OperationRequest(
            request_id=generate_uuid(),
            requesting_agent=self.agent_id,
            parent_agent="deepagent-master",
            task_id="analyze-sales-q4",
            task_description="Save analysis results",
            operation=OperationType.WRITE,
            paths=["/workspace/analysis/sales_q4_results.json"],
            reason="Save analysis results for report generation"
        )
        
        write_response = self.task_runner.execute_operation(write_request)
        
        return {
            "status": "success",
            "analysis": analysis_results,
            "saved_to": "/workspace/analysis/sales_q4_results.json"
        }
```

### 3. Path Validation Implementation

```python
import os
from pathlib import Path

def canonicalize_path(path: str) -> str:
    """Convert path to canonical absolute form"""
    # Resolve to absolute path
    abs_path = os.path.abspath(path)
    
    # Resolve symlinks
    real_path = os.path.realpath(abs_path)
    
    # Normalize (remove redundant separators, etc.)
    canonical = os.path.normpath(real_path)
    
    return canonical

def is_path_traversal_attack(original: str, canonical: str) -> bool:
    """Detect if original path attempts to escape intended directory"""
    
    # Check for suspicious patterns
    suspicious_patterns = [
        "..",      # Parent directory reference
        "~",       # Home directory expansion
        "//",      # Double slashes (could be UNC path)
    ]
    
    for pattern in suspicious_patterns:
        if pattern in original:
            # Not automatically an attack, but needs verification
            pass
    
    # More robust check: compare directory nesting depth
    # A path traversal attack will have fewer segments in canonical form
    original_depth = len(Path(original).parts)
    canonical_depth = len(Path(canonical).parts)
    
    # If canonical has fewer parts, path likely escaped upward
    if canonical_depth < original_depth - 1:  # Allow 1 level (relative paths)
        return True
    
    return False

def detect_zone(canonical_path: str) -> FilesystemZone:
    """Determine which zone the path belongs to"""
    
    # Check system-critical first (highest priority)
    system_critical_prefixes = [
        "/etc", "/sys", "/proc", "/dev", "/boot", 
        "/root", "/usr/bin", "/usr/sbin", "/var/log"
    ]
    for prefix in system_critical_prefixes:
        if canonical_path.startswith(prefix):
            return FilesystemZone.SYSTEM_CRITICAL
    
    # Check sandbox
    if canonical_path.startswith("/sandbox"):
        # Verify it hasn't escaped sandbox via symlink
        if not is_path_within_directory(canonical_path, "/sandbox"):
            return FilesystemZone.EXTERNAL  # Treat as external if escaped
        return FilesystemZone.SANDBOX
    
    # Check workspace
    if canonical_path.startswith("/workspace"):
        if not is_path_within_directory(canonical_path, "/workspace"):
            return FilesystemZone.EXTERNAL
        return FilesystemZone.WORKSPACE
    
    # Everything else is external
    return FilesystemZone.EXTERNAL

def is_path_within_directory(path: str, directory: str) -> bool:
    """Verify that path is truly within directory (no escapes)"""
    path_obj = Path(path).resolve()
    dir_obj = Path(directory).resolve()
    
    try:
        path_obj.relative_to(dir_obj)
        return True
    except ValueError:
        return False
```

### 4. Audit Logging

```python
import json
import logging
from datetime import datetime

class AuditLogger:
    """Audit trail for all filesystem operations"""
    
    def __init__(self, log_file: str = "/var/log/task-runner-agent/audit.log"):
        self.logger = logging.getLogger("TaskRunnerAudit")
        handler = logging.FileHandler(log_file)
        handler.setFormatter(
            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        )
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)
    
    def log_operation(
        self,
        request: OperationRequest,
        response: OperationResponse,
        zone: FilesystemZone,
        action: PermissionAction,
        user_decision: Optional[str] = None
    ):
        """Log a complete operation with context"""
        
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "operation_id": response.operation_id,
            "request_id": request.request_id,
            "agent": request.requesting_agent,
            "parent_agent": request.parent_agent,
            "task_id": request.task_id,
            "operation": request.operation.value,
            "paths": request.paths,
            "zone": zone.value,
            "action_taken": action.value,
            "user_decision": user_decision,
            "status": response.status,
            "error": response.error
        }
        
        # Log at appropriate level
        if response.status == "denied":
            self.logger.warning(json.dumps(log_entry))
        elif response.status == "error":
            self.logger.error(json.dumps(log_entry))
        else:
            self.logger.info(json.dumps(log_entry))
    
    def log_security_event(self, event_type: str, details: dict):
        """Log security-related events (path traversal attempts, etc.)"""
        
        security_event = {
            "timestamp": datetime.utcnow().isoformat(),
            "event_type": event_type,
            "severity": "CRITICAL",
            **details
        }
        
        self.logger.critical(json.dumps(security_event))
```

---

## Security Considerations

### 1. Path Traversal Prevention

**Attack Vectors:**
```bash
# Attacker tries to escape sandbox
/sandbox/../../../etc/passwd
/sandbox/../../../../home/user/.ssh/id_rsa
/workspace/../../../sensitive_data

# Symlink attacks
/sandbox/link_to_etc -> /etc
/workspace/escape -> /home/user/sensitive/
```

**Mitigations:**
- ✅ Always canonicalize paths before zone detection
- ✅ Resolve symlinks to real paths
- ✅ Verify resolved path is still within expected zone
- ✅ Block operations if path escapes designated zone
- ✅ Log all path traversal attempts as security events

### 2. Time-of-Check-to-Time-of-Use (TOCTOU) Race Conditions

**Attack Vector:**
```python
# Agent checks if file exists in workspace (allowed)
# Between check and execution, symlink is swapped to point to /etc/passwd
# Operation executes on /etc/passwd instead
```

**Mitigations:**
- ✅ Re-validate path immediately before execution
- ✅ Use file descriptors instead of paths when possible
- ✅ Execute operations atomically
- ✅ Lock files during multi-step operations

### 3. Resource Exhaustion

**Attack Vectors:**
- Malicious agent creates thousands of files in sandbox
- Agent requests permission for 10,000 files (spam user)
- Infinite loop in sandbox execution

**Mitigations:**
- ✅ Rate limiting: max 100 requests/minute per agent
- ✅ Sandbox quotas: max 10GB storage, 5 minute execution time
- ✅ Batch permission requests: group similar operations
- ✅ Operation timeouts: kill long-running operations

### 4. Information Leakage

**Attack Vectors:**
- Error messages reveal sensitive path information
- Timing attacks (does file exist?)
- Agent inference from denial patterns

**Mitigations:**
- ✅ Generic error messages for external zones
- ✅ Consistent timing for permission checks
- ✅ Don't reveal whether file exists if access denied
- ✅ Audit logs stored securely (not accessible to agents)

### 5. Privilege Escalation

**Attack Vectors:**
- Sub-agent impersonates parent agent to gain permissions
- Agent claims to be part of different task to bypass limits
- Replay attack using old permission grants

**Mitigations:**
- ✅ Verify agent identity via cryptographic signatures
- ✅ Permission grants are single-use with unique request_id
- ✅ Task IDs validated against parent agent records
- ✅ No inheritance of permissions between tasks

### 6. Sandbox Escape

**Attack Vectors:**
- Code execution exploits kernel vulnerabilities
- Mount operations to access host filesystem
- Network access to exfiltrate data

**Mitigations:**
- ✅ Sandbox runs in isolated container (no network by default)
- ✅ Restricted syscalls (no mount, no device access)
- ✅ Read-only filesystem except for /sandbox
- ✅ Execution time limits
- ✅ Monitor for suspicious syscalls

---

## Usage Examples

### Example 1: Research Agent Analyzing External Data

```python
# User Request: "Analyze the sales data in my Downloads folder"

# Step 1: Master Agent receives request
master_agent = DeepAgent()
master_agent.receive_user_request(
    "Analyze sales data in /home/user/Downloads/sales_2025.xlsx"
)

# Step 2: Master spawns Research Agent
research_agent = master_agent.spawn_sub_agent(
    agent_type="ResearchAgent",
    task="Analyze sales trends from Excel file"
)

# Step 3: Research Agent requests READ via Task Runner
task_runner = TaskRunnerAgent()

read_request = OperationRequest(
    request_id="req_001",
    requesting_agent="research-agent-001",
    parent_agent="deepagent-master",
    task_id="task_sales_analysis",
    task_description="Analyze Q4 2025 sales trends and generate report",
    operation=OperationType.READ,
    paths=["/home/user/Downloads/sales_2025.xlsx"],
    reason="Need to read sales data for trend analysis"
)

# Step 4: Task Runner detects EXTERNAL zone, prompts user
# User sees:
"""
🤖 Agent Permission Request

Requesting Agent: research-agent-001
Parent Agent: deepagent-master
Task: Analyze Q4 2025 sales trends and generate report
Operation: READ
Target Paths: /home/user/Downloads/sales_2025.xlsx
Reason: Need to read sales data for trend analysis
Risk Level: LOW

Alternatives:
- Copy to /workspace for safer access

[✅ Allow]  [🚫 Deny]  [📋 Allow with Copy to Workspace]
"""

# User clicks "Allow with Copy to Workspace"

# Step 5: Task Runner copies file to workspace and returns path
response = OperationResponse(
    request_id="req_001",
    status="success",
    result={
        "action": "copied_to_workspace",
        "new_path": "/workspace/input/sales_2025.xlsx",
        "original_path": "/home/user/Downloads/sales_2025.xlsx"
    },
    operation_id="op_001"
)

# Step 6: Research Agent analyzes data from workspace copy
data = research_agent.read_from_workspace("/workspace/input/sales_2025.xlsx")
analysis = research_agent.analyze(data)

# Step 7: Research Agent writes results to workspace (auto-allowed)
write_request = OperationRequest(
    request_id="req_002",
    requesting_agent="research-agent-001",
    parent_agent="deepagent-master",
    task_id="task_sales_analysis",
    task_description="Save analysis results",
    operation=OperationType.WRITE,
    paths=["/workspace/reports/sales_analysis_2025.json"],
    reason="Store analysis results for report generation"
)

# No prompt needed (workspace write is auto-allowed)
write_response = task_runner.execute_operation(write_request)
# Returns: status="success"

# Step 8: Research Agent reports back to master
research_agent.report_to_parent({
    "status": "complete",
    "results": analysis,
    "output_file": "/workspace/reports/sales_analysis_2025.json"
})

# Step 9: Master Agent reports to user
master_agent.respond_to_user(
    "Analysis complete! Results saved to workspace. Key findings: ..."
)
```

### Example 2: Code Execution in Sandbox

```python
# User Request: "Write a Python script to clean this messy CSV and run it"

# Master Agent spawns Code Generation Agent
code_agent = master_agent.spawn_sub_agent("CodeGenerationAgent")

# Code Agent generates Python script
script_content = """
import pandas as pd
import sys

# Read messy CSV
df = pd.read_csv(sys.argv[1])

# Clean data
df = df.dropna()
df = df.drop_duplicates()

# Save cleaned version
df.to_csv(sys.argv[2], index=False)
print(f"Cleaned {len(df)} rows")
"""

# Code Agent writes script to SANDBOX (auto-allowed)
create_request = OperationRequest(
    request_id="req_010",
    requesting_agent="code-agent-001",
    parent_agent="deepagent-master",
    task_id="task_clean_csv",
    task_description="Create data cleaning script",
    operation=OperationType.CREATE,
    paths=["/sandbox/clean_csv.py"],
    reason="Generate script to clean messy CSV data"
)

task_runner.execute_operation(create_request)
# Auto-allowed (sandbox zone)

# Code Agent executes script in SANDBOX (auto-allowed)
execute_request = OperationRequest(
    request_id="req_011",
    requesting_agent="code-agent-001",
    parent_agent="deepagent-master",
    task_id="task_clean_csv",
    task_description="Execute data cleaning script",
    operation=OperationType.EXECUTE,
    paths=["/sandbox/clean_csv.py"],
    reason="Run script to clean CSV file",
    additional_context={
        "command": "python /sandbox/clean_csv.py /workspace/input.csv /workspace/output.csv",
        "timeout": 60
    }
)

exec_response = task_runner.execute_operation(execute_request)
# Auto-allowed (sandbox execution)

# Script runs, accesses workspace files (read/write auto-allowed)
# Result: Cleaned CSV saved to workspace
```

### Example 3: Denied Operation with Alternatives

```python
# Research Agent tries to delete important file

delete_request = OperationRequest(
    request_id="req_020",
    requesting_agent="research-agent-001",
    parent_agent="deepagent-master",
    task_id="task_cleanup",
    task_description="Clean up temporary files",
    operation=OperationType.DELETE,
    paths=["/home/user/Documents/important_report.pdf"],
    reason="Remove temporary analysis file"
)

# Task Runner detects EXTERNAL + DELETE (high risk)
# Prompts user:
"""
⚠️ DESTRUCTIVE OPERATION WARNING

Requesting Agent: research-agent-001
⚠️ WILL DELETE: /home/user/Documents/important_report.pdf
THIS CANNOT BE UNDONE!
File Size: 2.3 MB
Reason: Remove temporary analysis file

[🚫 Deny (Recommended)]  [✅ Allow (I understand the risk)]
"""

# User clicks "Deny"

# Task Runner returns denial with alternatives
response = OperationResponse(
    request_id="req_020",
    status="denied",
    error="User denied permission to delete external file",
    alternatives=[
        "Delete files in /workspace or /sandbox instead",
        "If this is truly temporary, it should be in /sandbox",
        "Workspace files can be deleted with user confirmation"
    ],
    operation_id="op_020"
)

# Research Agent handles denial
research_agent.handle_denied_operation(response)
# Agent adapts: uses /sandbox for temp files in future
```

---

## Appendix

### A. Common Operation Patterns

| Agent Task | Operations Needed | Zone(s) | User Prompts |
|------------|-------------------|---------|--------------|
| Analyze user's file | READ (external) → process → WRITE (workspace) | External + Workspace | 1 (read) |
| Generate report | WRITE (workspace) | Workspace | 0 |
| Run test script | CREATE + EXECUTE (sandbox) | Sandbox | 0 |
| Clean up old files | DELETE (workspace) | Workspace | 1 per file |
| Install package | EXECUTE (sandbox) + WRITE (workspace) | Sandbox + Workspace | 0 |
| Backup to external | READ (workspace) → WRITE (external) | Workspace + External | 1 (write) |

### B. Error Codes

```yaml
error_codes:
  TR001: "Path traversal attack detected"
  TR002: "System-critical path access denied"
  TR003: "User denied permission"
  TR004: "Invalid operation for zone"
  TR005: "Path does not exist"
  TR006: "Rate limit exceeded"
  TR007: "Operation timeout"
  TR008: "Insufficient disk space"
  TR009: "Permission verification failed"
  TR010: "Agent authentication failed"
```

### C. Performance Considerations

**Expected Latencies:**
- Sandbox operation: < 50ms (auto-allowed)
- Workspace operation: < 100ms (auto-allowed)
- External operation (with prompt): 2-30 seconds (user dependent)
- Delete operation: 1-10 seconds (user confirmation)

**Optimization Strategies:**
- Cache zone detection results for frequently accessed paths
- Batch multiple operations from same agent/task
- Pre-authorize common patterns (e.g., "allow all reads from /home/user/Documents")
- Parallel permission requests when operations are independent

### D. Future Enhancements

**Planned Features:**
- **Permission Templates**: User-defined rules for specific agents or tasks
- **Learning Mode**: System learns from user decisions to auto-approve safe patterns
- **Rollback**: Undo filesystem changes if task fails
- **Diff Preview**: Show exact changes before approving write/delete operations
- **Agent Reputation**: Track agent behavior, auto-allow for trusted agents
- **Encrypted Zones**: Support for encrypted workspace/sandbox
- **Remote Filesystems**: Support for cloud storage (S3, Google Drive, etc.)

---

## Conclusion

This Task Runner Agent architecture provides:

✅ **Security**: Multiple layers of defense against malicious or buggy agents  
✅ **Usability**: Clear permission model that users can understand  
✅ **Scalability**: Works with any number of agents and tasks  
✅ **Flexibility**: Supports diverse use cases from code execution to data analysis  
✅ **Auditability**: Complete trail of all filesystem operations  

The key insight is treating the Task Runner Agent as a **capability gateway** rather than just a filesystem wrapper. By centralizing all filesystem operations through a single, well-defined interface, the system gains:

- Consistent security enforcement
- Better user experience (fewer confusing prompts)
- Easier debugging (centralized logs)
- Simple agent development (agents never touch filesystem directly)

**Next Steps:**
1. Implement core Task Runner Agent with basic zone detection
2. Add comprehensive test suite for path validation
3. Build user interface for permission requests
4. Deploy with logging and monitoring
5. Iterate based on real-world usage patterns

---

**Document Version:** 1.0  
**Last Updated:** April 20, 2026  
**Maintained By:** Architecture Team  
**License:** Internal Use Only
