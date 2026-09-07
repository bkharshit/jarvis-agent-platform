"""Fixture strategy plugins for JARVIS (S3).

`plan_execute` is the sample the plugin contract doc walks through;
`raise_plugin` is the malformed-plugin probe. Both live behind the
`jarvis.strategies` entry-point group and load only when allow-listed."""

__all__ = ["PlanExecuteStrategy", "RaisePluginStrategy"]
