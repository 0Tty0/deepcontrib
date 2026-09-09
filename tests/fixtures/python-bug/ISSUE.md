# Normalize repeated spaces in generated slugs

`slugify` currently emits multiple separators when a title contains repeated
spaces. Collapse runs of whitespace into one separator while preserving the
existing lower-case behavior. Add or update a regression test for the case
`Hello,  World!`.
