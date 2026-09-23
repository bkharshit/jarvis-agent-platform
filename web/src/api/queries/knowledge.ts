import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";

import type { components } from "@/api/schema";

// Knowledge datasets (S8, ADR 0020 §6, D63/D65): tenant-scoped resources
// with the eval-dataset discipline — a PATCH is a wholesale mutable-fields
// document (name/description only; any config field 422s at the schema
// boundary), and ingest is SYNCHRONOUS (chunk → embed → persist in one
// transaction, a 502 carrying an honest persisted `error` row).

export type KnowledgeDataset = components["schemas"]["KnowledgeDataset"];
export type KnowledgeDatasetCreate = components["schemas"]["KnowledgeDatasetCreate"];
export type KnowledgeDatasetUpdate = components["schemas"]["KnowledgeDatasetUpdate"];
export type KnowledgeDocument = components["schemas"]["KnowledgeDocument"];
export type KnowledgeDocumentCreate = components["schemas"]["KnowledgeDocumentCreate"];
export type EmbeddingConfig = components["schemas"]["EmbeddingConfig"];
export type ChunkConfig = components["schemas"]["ChunkConfig"];

export const knowledgeDatasetsQueryKey = ["knowledge", "datasets"] as const;
export const knowledgeDatasetQueryKey = (id: string) => ["knowledge", "datasets", id] as const;
export const knowledgeDocumentsQueryKey = (id: string) =>
  ["knowledge", "datasets", id, "documents"] as const;

export function useKnowledgeDatasets() {
  return useQuery({
    queryKey: knowledgeDatasetsQueryKey,
    queryFn: async () => {
      const body = await unwrap(client.GET("/v1/knowledge/datasets"));
      return body.items;
    },
  });
}

export function useKnowledgeDataset(datasetId: string) {
  return useQuery({
    queryKey: knowledgeDatasetQueryKey(datasetId),
    enabled: datasetId !== "",
    queryFn: async () =>
      unwrap(
        client.GET("/v1/knowledge/datasets/{dataset_id}", {
          params: { path: { dataset_id: datasetId } },
        }),
      ),
  });
}

export function useCreateKnowledgeDataset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: KnowledgeDatasetCreate) =>
      unwrap(client.POST("/v1/knowledge/datasets", { body })),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: knowledgeDatasetsQueryKey }),
  });
}

export function useUpdateKnowledgeDataset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ datasetId, body }: { datasetId: string; body: KnowledgeDatasetUpdate }) =>
      unwrap(
        client.PATCH("/v1/knowledge/datasets/{dataset_id}", {
          params: { path: { dataset_id: datasetId } },
          body,
        }),
      ),
    onSuccess: (_data, vars) => {
      void queryClient.invalidateQueries({ queryKey: knowledgeDatasetsQueryKey });
      void queryClient.invalidateQueries({ queryKey: knowledgeDatasetQueryKey(vars.datasetId) });
    },
  });
}

export function useDeleteKnowledgeDataset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (datasetId: string) =>
      unwrap(
        client.DELETE("/v1/knowledge/datasets/{dataset_id}", {
          params: { path: { dataset_id: datasetId } },
        }),
      ),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: knowledgeDatasetsQueryKey }),
  });
}

export function useKnowledgeDocuments(datasetId: string) {
  return useQuery({
    queryKey: knowledgeDocumentsQueryKey(datasetId),
    enabled: datasetId !== "",
    queryFn: async () => {
      const body = await unwrap(
        client.GET("/v1/knowledge/datasets/{dataset_id}/documents", {
          params: { path: { dataset_id: datasetId } },
        }),
      );
      return body.items;
    },
  });
}

export function useCreateKnowledgeDocument(datasetId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: KnowledgeDocumentCreate) =>
      unwrap(
        client.POST("/v1/knowledge/datasets/{dataset_id}/documents", {
          params: { path: { dataset_id: datasetId } },
          body,
        }),
      ),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: knowledgeDocumentsQueryKey(datasetId) }),
  });
}

export function useDeleteKnowledgeDocument(datasetId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (documentId: string) =>
      unwrap(
        client.DELETE("/v1/knowledge/datasets/{dataset_id}/documents/{document_id}", {
          params: { path: { dataset_id: datasetId, document_id: documentId } },
        }),
      ),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: knowledgeDocumentsQueryKey(datasetId) }),
  });
}
