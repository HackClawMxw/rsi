# Recorded observer recipe

The established loader adapter convention exposes `(param, weight, **kwargs)`.
Use the following starting recipe when creating the observer:

```python
def wrap(loader):
    def observed(param, weight, **kwargs):
        return loader(param, weight, **kwargs)
    return observed
```

Run the fixed local protocol fixture before considering the observer complete.
If the fixture fails, inspect the actual invocation and exception, preserve the
failure evidence, and repair the observer without changing the original loader
or the fixture. A wrapper error does not establish a production-model defect.
