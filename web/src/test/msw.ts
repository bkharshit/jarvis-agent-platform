import { setupServer } from "msw/node";

import { handlers } from "./handlers";

// One shared msw server for the suite; tests override handlers per-case.
export const server = setupServer(...handlers);