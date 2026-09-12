export type CompletedImageLike = {
  complete: boolean;
  naturalWidth: number;
};

/**
 * O pacote não distribui imagens decorativas obrigatórias. Manter os fallbacks
 * vazios evita requests 404; instalações podem fornecer URLs por VITE_*.
 */
export const BUNDLED_DECORATIVE_IMAGE_URL = "";
export const BUNDLED_LOADING_GIF_URL = "";

export function resolveCompletedImageState(
  image: CompletedImageLike | null,
): "ready" | "failed" | null {
  if (!image?.complete) return null;
  return image.naturalWidth > 0 ? "ready" : "failed";
}
