export type CompletedImageLike = {
  complete: boolean;
  naturalWidth: number;
};

/**
 * Os assets de public/assets são copiados pelo Vite para /assets no build.
 * Instalações podem substituir as URLs por VITE_*; os componentes mantêm
 * seus fallbacks se os arquivos estiverem ausentes em um pacote de código.
 */
export const BUNDLED_DECORATIVE_IMAGE_URL = "/assets/osaka-landing-character.jpg";
export const BUNDLED_LOADING_GIF_URL = "/assets/osaka-loading.gif";

export function resolveCompletedImageState(
  image: CompletedImageLike | null,
): "ready" | "failed" | null {
  if (!image?.complete) return null;
  return image.naturalWidth > 0 ? "ready" : "failed";
}
