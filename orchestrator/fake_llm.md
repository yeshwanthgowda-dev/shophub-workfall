# FakeChatModel internals

Diagrams for `orchestrator/fake_llm.py`. The model keeps no memory except `calls`. Every reply is chosen by rule from the messages passed in.

## 1. Entry: from the graph to a reply

```mermaid
flowchart TD
    A["Graph calls ainvoke / astream / invoke"] --> B{"Streaming?"}
    B -- "no" --> C["_generate"]
    B -- "yes" --> D["_astream / _stream -> _chunks"]
    C --> E["_choose_reply(messages)"]
    D --> E
    E --> F["Append the batch to self.calls"]
    F --> G{"Type of last message"}
    G -- "ToolMessage" --> H["_reply_to_tool_result"]
    G -- "HumanMessage" --> I["_reply_to_human"]
    G -- "anything else" --> J["AIMessage: How can I help with your orders?"]
    H --> K["AIMessage (text or tool_calls)"]
    I --> K
    J --> K
    K --> L["_add_usage: fake token counts from word counts"]
    L --> M{"Which path?"}
    M -- "_generate" --> N["Return ChatResult with the AIMessage"]
    M -- "_chunks" --> O{"Has tool_calls?"}
    O -- "yes" --> P["Yield one chunk per tool call"]
    O -- "no" --> Q["Yield the text word by word"]
    P --> R["Yield a final chunk with usage_metadata"]
    Q --> R
```

## 2. `_reply_to_human`: the last message is from the user

`_tool_accessible(x)` means tool `x` is in `bound_tools`. An unbound tool is never called.

```mermaid
flowchart TD
    A["text = latest human message"] --> B["order_id = _find_order_id: latest message first, else carried over from an earlier one"]
    B --> C{"refund / reimburse / money back in text?"}
    C -- "yes" --> C1{"order_id found and propose_refund accessible?"}
    C1 -- "yes" --> C2["Tool call: propose_refund(order_id)"]
    C1 -- "no" --> C3["Text: Which order id should I use?"]
    C -- "no" --> D{"cancel in text?"}
    D -- "yes" --> D1{"order_id found and propose_cancel accessible?"}
    D1 -- "yes" --> D2["Tool call: propose_cancel(order_id)"]
    D1 -- "no" --> C3
    D -- "no" --> E{"order_id and note / deliver / why / reason / request in text, and get_order_note accessible?"}
    E -- "yes" --> E1["Tool call: get_order_note(order_id)"]
    E -- "no" --> F{"order_id and get_order accessible?"}
    F -- "yes" --> F1["Tool call: get_order(order_id)"]
    F -- "no" --> G{"Name pattern matched and find_orders_by_customer_name accessible?"}
    G -- "yes" --> G1["Tool call: find_orders_by_customer_name(name)"]
    G -- "no" --> H{"list_orders accessible and order / status word in text?"}
    H -- "yes" --> H1["Tool call: list_orders(status?, older_than_days?)"]
    H -- "no" --> I["Text: I can help with orders. Ask about an order id, a status, or a customer name."]
```

## 3. `_reply_to_tool_result`: the last message is a tool result

```mermaid
flowchart TD
    A["Join the trailing ToolMessages into one string: content"] --> B{"gullible and content looks like an injection?"}
    B -- "yes" --> B1{"Order id found in content?"}
    B1 -- "yes" --> B2["Tool call: gullible_tool(order_id), default refund_order, never a bound tool"]
    B1 -- "no" --> C
    B -- "no" --> C{"has_note is true in content, get_order_note not yet called this turn, human text mentions a note, and get_order_note accessible?"}
    C -- "yes" --> C1{"Order id found in content?"}
    C1 -- "yes" --> C2["Tool call: get_order_note(order_id)"]
    C1 -- "no" --> D
    C -- "no" --> D["Text: Here is what I found: first 500 characters of content"]
```

## Where each reply goes next

- A reply with `tool_calls` goes to the graph's routing check, which sends it to the `tools` node (or the `propose` node for `propose_*`).
- A text reply with no `tool_calls` ends the graph, and the text is streamed to the user.
