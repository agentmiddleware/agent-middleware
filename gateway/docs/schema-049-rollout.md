# Schema 049: demo key attribution (ported)

Migration `049_demo_key_attribution` revises `048_tenant_rls`, ported with the
same revision id. `043_demo_key_fields` already adds the UTM and referrer
columns here, so on this chain it adds only missing columns (normally none).

## Rollback boundary

Downgrade is a no-op; the columns belong to 043, which retains them.
