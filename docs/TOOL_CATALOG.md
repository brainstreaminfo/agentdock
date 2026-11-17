# Tool Catalog: how it works and how to add a tool

Agents never call Shopify directly. They call **tools**: small, typed Python
functions (or staff-made GraphQL tools) that all go through one gatekeeper,
`apps/tools/executor.py`.

```
AI asks for a tool ──► execute_tool(ctx, "products.update", {...})
                         1. tool exists and is enabled?
                         2. shop granted the scopes?   ── no ──► error + "grant access" alert
                         3. input valid?                ── no ──► error the AI can fix
                         4. changes data?               ── yes ─► ActionProposal (waits for
                                                                  approval unless AUTO)
                         5. run it (GraphQL cost / throttling handled by the client)
                         6. result framed as untrusted data, emails/phones redacted
                         7. RunStep saved (input, output, duration, error)
```

## Where things are

| Path | What |
|---|---|
| `apps/tools/registry.py` | `@tool` decorator, `ToolInput` base class |
| `apps/tools/context.py` | `ToolContext`: `ctx.shop`, `ctx.gql(...)`, `ctx.require_scopes(...)` |
| `apps/tools/executor.py` | the rules above + `apply_proposal` / `reject_proposal` / `undo_proposal` |
| `apps/tools/catalog/<domain>.py` + `.graphql` | the 50 built-in tools |
| `apps/tools/custom.py` | custom GraphQL tools: validation + running |
| `/studio/tools/` | browse, enable/disable, create custom tools, **Test** |

## Add a built-in tool (example: count products)

1. **GraphQL**: add an operation to `apps/tools/catalog/products.graphql`:

   ```graphql
   query CountProducts($query: String) {
     productsCount(query: $query) { count }
   }
   ```

2. **Python**: add the tool to `apps/tools/catalog/products.py`:

   ```python
   class CountInput(ToolInput):
       status: Literal["ACTIVE", "DRAFT", "ARCHIVED"] | None = None


   @tool(
       key="products.count",
       name="Count products",
       description="How many products the store has, optionally by status.",
       category="Products",
       scopes=["read_products"],
   )
   def count_products(ctx: ToolContext, args: CountInput) -> dict:
       query = f"status:{args.status.lower()}" if args.status else None
       data = ctx.gql(DOC, "CountProducts", {"query": query})
       return {"count": data["productsCount"]["count"]}
   ```

3. **Tell the integrity test about it**: add `"products.count"` to `SPEC_TOOLS`
   in `tests/tools/test_catalog_integrity.py`.

4. **Test it** in `tests/tools/test_tools_shopify.py`:

   ```python
   def test_products_count(ctx, fake_shopify):
       fake_shopify.on("CountProducts", {"productsCount": {"count": 12}})
       assert execute_tool(ctx, "products.count", {"status": "ACTIVE"}).data == {"count": 12}
   ```

5. Run `make check`. The tests validate your GraphQL against Shopify's schema
   and check the variables you send. On deploy, `sync_tools` adds the tool to
   the Studio.

### A tool that changes data

Set `mutating=True` and declare a `write_*` scope, then add three small
functions on the tool (see `products.update` for a full example):

```python
@my_tool.preview      # what the merchant approves: summary + before/after changes
@my_tool.snapshot     # store data right before the change (saved for undo)
@my_tool.undo         # (ctx, args, before, after): put things back
```

Raise `ToolError("clear message")` for anything the AI should know about
(not found, wrong id…). Pass `user_errors_at="mutationName"` to `ctx.gql` so
Shopify's validation errors become readable tool errors.

## Custom GraphQL tools (no code)

Studio → Tool catalog → **Custom GraphQL tool**. Enter one query or mutation, a
JSON Schema for its variables, and the scopes it needs. On save it is checked
against Shopify's real schema. Mutations are automatically marked "changes
data" (so they need approval) and can't be undone. Use **Test** on the tool's
page to run it against your dev store.
