/**
 * Document submission composable.
 * Handles the submit flow from Vue components.
 */
import { ref } from "vue";

export function useDocumentSubmission() {
  const submitResult = ref(null);
  const submitError = ref(null);

  async function submit(submitFn) {
    submitError.value = null;
    submitResult.value = null;
    try {
      const result = await submitFn();
      submitResult.value = result;
      return result;
    } catch (e) {
      submitError.value = e.message || "Submit failed";
      throw e;
    }
  }

  return {
    submitResult,
    submitError,
    submit,
  };
}
