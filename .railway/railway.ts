import { defineRailway, project, service } from "railway/iac";

// Last resort for a per-service CaC repo. Prefer one .railway file for the
// project and drop this if you later combine services into that file.
export const partial = "graphsleuth";

export default defineRailway(() => {
  const graphsleuth = service("graphsleuth", {
    start: "/app/docker-entrypoint.sh",
    healthcheck: "/api/meta",
    healthcheckTimeout: 120,
    dockerfilePath: "Dockerfile",
    builder: "dockerfile",
  });
  return project("graphsleuth", {
    resources: [graphsleuth],
  });
});
