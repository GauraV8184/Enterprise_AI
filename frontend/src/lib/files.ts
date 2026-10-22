/** File validation shared by the upload dropzone. */

import { ACCEPTED_EXTENSIONS, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB } from "./config";
import { formatBytes } from "./format";

export interface FileValidation {
  valid: boolean;
  reason?: string;
}

export function validateFile(file: File): FileValidation {
  const dot = file.name.lastIndexOf(".");
  const extension = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";

  if (!(ACCEPTED_EXTENSIONS as readonly string[]).includes(extension)) {
    return {
      valid: false,
      reason: `Unsupported type. Use ${ACCEPTED_EXTENSIONS.join(", ")}.`,
    };
  }
  if (file.size === 0) {
    return { valid: false, reason: "The file is empty." };
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return {
      valid: false,
      reason: `File is ${formatBytes(file.size)}; the limit is ${MAX_UPLOAD_MB} MB.`,
    };
  }
  return { valid: true };
}
