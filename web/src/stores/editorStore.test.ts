import { describe, expect, it } from "vitest";

import { agentFixture } from "@/test/handlers";

import { draftForCreate, draftFromDefinition, toSavePayload, useEditorStore } from "./editorStore";

describe("toSavePayload", () => {
  it("omits empty optional fields entirely (never null, never undefined)", () => {
    const draft = {
      ...draftForCreate(),
      name: "  My agent  ",
      model: {
        provider: "mock",
        model: "m1",
        base_url: "",
        credential_kind: "" as const,
        credential_value: "",
      },
      user_prompt_template: "",
      memory: { enabled: false, max_messages: 20, session_key: "" },
      output_schema: "",
    };
    const { body, error } = toSavePayload(draft);
    expect(error).toBeUndefined();
    const raw = JSON.parse(JSON.stringify(body));
    expect(raw.name).toBe("My agent");
    expect("user_prompt_template" in raw).toBe(false);
    expect("base_url" in raw.model).toBe(false);
    expect("credential_ref" in raw.model).toBe(false);
    expect("session_key" in raw.memory).toBe(false);
    expect("output_schema" in raw).toBe(false);
  });

  it("includes optional fields when non-empty", () => {
    const draft = {
      ...draftForCreate(),
      name: "a",
      user_prompt_template: "Do {{input}}",
      output_schema: '{"type":"object"}',
    };
    const { body, error } = toSavePayload(draft);
    expect(error).toBeUndefined();
    expect(body.user_prompt_template).toBe("Do {{input}}");
    expect(body.output_schema).toEqual({ type: "object" });
  });

  it("reports an error for invalid JSON fields instead of saving", () => {
    const bad = { ...draftForCreate(), name: "a", strategy: { type: "react" as const, params: "{oops" } };
    expect(toSavePayload(bad).error).toMatch(/strategy params/);
    const badSchema = { ...draftForCreate(), name: "a", output_schema: "[1,2]" };
    expect(toSavePayload(badSchema).error).toMatch(/must be a JSON object/);
  });

  it("requires a non-blank name", () => {
    expect(toSavePayload({ ...draftForCreate(), name: "   " }).error).toMatch(/name is required/);
  });

  it("builds the credential_ref union (S2) — env, stored, or omitted", () => {
    const env = {
      ...draftForCreate(),
      name: "a",
      model: { provider: "mock", model: "m1", base_url: "", credential_kind: "env" as const, credential_value: " OPENAI_API_KEY " },
    };
    const saved = toSavePayload(env);
    expect(saved.error).toBeUndefined();
    expect(saved.body.model?.credential_ref).toEqual({ type: "env", env_var: "OPENAI_API_KEY" });

    const stored = {
      ...draftForCreate(),
      name: "a",
      model: { provider: "mock", model: "m1", base_url: "", credential_kind: "stored" as const, credential_value: "cred-1" },
    };
    expect(toSavePayload(stored).body.model?.credential_ref).toEqual({
      type: "stored",
      credential_id: "cred-1",
    });

    // a chosen kind with no value is a form error, never a half-written ref
    const emptyValue = { ...draftForCreate(), name: "a", model: { provider: "mock", model: "m1", base_url: "", credential_kind: "env" as const, credential_value: "" } };
    expect(toSavePayload(emptyValue).error).toMatch(/credential/);
  });

  it("round-trips a stored credential ref from a definition", () => {
    const draft = draftFromDefinition({
      ...agentFixture,
      model: {
        ...agentFixture.model,
        credential_ref: { type: "env", env_var: "OPENAI_API_KEY" },
      },
    });
    const { body, error } = toSavePayload(draft);
    expect(error).toBeUndefined();
    expect(body.model?.credential_ref).toEqual({ type: "env", env_var: "OPENAI_API_KEY" });
  });

  it("round-trips a definition draft through the payload", () => {
    const draft = draftFromDefinition(agentFixture);
    const { body, error } = toSavePayload(draft);
    expect(error).toBeUndefined();
    expect(body.name).toBe(agentFixture.name);
    expect(body.model?.provider).toBe("mock");
    expect(body.tools).toEqual(agentFixture.tools);
    expect(body.strategy?.type).toBe("function_calling");
  });
});

describe("editor store", () => {
  it("opens for create and tracks dirtiness", () => {
    const { open, update } = useEditorStore.getState();
    open(null);
    expect(useEditorStore.getState().agentId).toBeNull();
    expect(useEditorStore.getState().isDirty).toBe(false);
    update({ name: "x" });
    expect(useEditorStore.getState().isDirty).toBe(true);
    expect(useEditorStore.getState().draft?.name).toBe("x");
  });

  it("opens from a definition and closes cleanly", () => {
    const { open, close } = useEditorStore.getState();
    open(agentFixture);
    expect(useEditorStore.getState().agentId).toBe("agent-1");
    close();
    expect(useEditorStore.getState().draft).toBeNull();
    expect(useEditorStore.getState().isDirty).toBe(false);
  });
});