import { Extension } from "@tiptap/core";

/**
 * WordWrap Tiptap Extension.
 *
 * Provides a CSS-only word-wrap toggle that does NOT persist
 * into the document data. It toggles a CSS class on the editor
 * wrapper element to switch between normal wrapping and
 * break-all / break-word behavior.
 *
 * This is purely a display preference and does not modify
 * node attributes or document content.
 */
var WordWrap = Extension.create({
  name: "wordWrap",

  addOptions: function () {
    return {
      // CSS class added to the editor when word-wrap is active
      wrapClass: "umo-word-wrap-enabled",
    };
  },

  addStorage: function () {
    return {
      enabled: false,
    };
  },

  onUpdate: function () {
    // No-op: word wrap is cosmetic only
  },

  addCommands: function () {
    return {
      /**
       * Toggle word-wrap mode. Toggles a CSS class on the editor DOM.
       * Does NOT modify document content or node attributes.
       */
      toggleWordWrap:
        function () {
          return function (ref) {
            var editor = ref.editor;
            var storage = editor.storage.wordWrap;
            var enabled = !storage.enabled;
            storage.enabled = enabled;

            var editorEl = editor.view.dom;
            if (editorEl) {
              if (enabled) {
                editorEl.classList.add(editor.storage.wordWrap.options.wrapClass || "umo-word-wrap-enabled");
              } else {
                editorEl.classList.remove(editor.storage.wordWrap.options.wrapClass || "umo-word-wrap-enabled");
              }
            }

            return true;
          };
        },
    };
  },
});

export default WordWrap;
