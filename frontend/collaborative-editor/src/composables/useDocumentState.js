/**
 * Document state composable.
 * Provides reactive save status for Vue components.
 */
 import { ref, readonly } from "vue";

 export function useDocumentState() {
   const saveStatus = ref("saved");
   const isDirty = ref(false);
   const isSubmitting = ref(false);
   const isReadOnly = ref(false);

   function updateSaveStatus(status) {
     saveStatus.value = status;
   }

   function markDirty() {
     isDirty.value = true;
     saveStatus.value = "unsaved";
   }

   function markSaved() {
     isDirty.value = false;
     saveStatus.value = "saved";
   }

   function markSaving() {
     saveStatus.value = "saving";
   }

   function setSubmitting(val) {
     isSubmitting.value = val;
   }

   function setReadOnly(val) {
     isReadOnly.value = val;
   }

   function pollNonReactive(getStatusFn) {
     const interval = setInterval(() => {
       const status = getStatusFn();
       if (status !== saveStatus.value) {
         saveStatus.value = status;
       }
     }, 500);
     return () => clearInterval(interval);
   }

   return {
     saveStatus: readonly(saveStatus),
     isDirty: readonly(isDirty),
     isSubmitting: readonly(isSubmitting),
     isReadOnly: readonly(isReadOnly),
     updateSaveStatus,
     markDirty,
     markSaved,
     markSaving,
     setSubmitting,
     setReadOnly,
     pollNonReactive,
   };
 }
