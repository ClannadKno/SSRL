import { Extension } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";

/**
 * FormatPainter Tiptap Extension.
 *
 * Copies from source:
 * - Marks: bold, italic, underline, strike, fontFamily, fontSize, color, highlight
 * - Block attributes: textAlign, lineHeight
 * - Block type: paragraph, heading (with level), blockquote
 *
 * Does NOT copy: link URL, task state, collaboration metadata, user info, internal IDs, code block language.
 *
 * Usage:
 *   1. Place cursor on formatted text and run `toggleFormatPainter` to capture.
 *   2. Select target text to apply formatting.
 *   3. Press Escape or run `unsetFormatPainter` to cancel.
 */
var FormatPainter = Extension.create({
  name: "formatPainter",

  addOptions: function () {
    return {
      // If true, format painter is single-use (clear after one application)
      once: true,
    };
  },

  addStorage: function () {
    return {
      enabled: false,
      once: true,
      marks: [],         // captured marks
      blockType: null,   // { type: 'heading', attrs: { level: 2 } } or { type: 'paragraph', attrs: {} }
      blockAttrs: {},    // { textAlign: 'center', lineHeight: '1.5' }
    };
  },

  addCommands: function () {
    return {
      /**
       * Toggle format painter: capture formatting from current selection.
       */
      toggleFormatPainter:
        function (once) {
          return function (ref) {
            var editor = ref.editor;
            var storage = editor.storage.formatPainter;
            var state = editor.state;

            // If already enabled, disable it
            if (storage.enabled) {
              storage.enabled = false;
              storage.marks = [];
              storage.blockType = null;
              storage.blockAttrs = {};
              editor.view.dispatch(
                editor.state.tr.setMeta("formatPainterAction", { type: "end" })
              );
              return true;
            }

            // Capture marks from current position
            var $from = state.selection.$from;
            var marks = $from.marks();
            storage.marks = marks.filter(function (m) {
              // Exclude link marks
              return m.type.name !== "link";
            });

            // Capture block type and attributes
            var resolvedNode = state.doc.nodeAt($from.before());
            if (!resolvedNode) {
              resolvedNode = $from.node();
            } else {
              // Try to get the parent block node
              var depth = $from.depth;
              for (var d = depth; d >= 0; d--) {
                var node = $from.node(d);
                if (node.type.isBlock && node.type.name !== "doc") {
                  resolvedNode = node;
                  break;
                }
              }
            }

            if (resolvedNode && resolvedNode.type.isBlock) {
              if (resolvedNode.type.name === "heading") {
                storage.blockType = {
                  type: "heading",
                  attrs: { level: resolvedNode.attrs.level || 1 },
                };
              } else if (resolvedNode.type.name === "paragraph") {
                storage.blockType = {
                  type: "paragraph",
                  attrs: {},
                };
              } else if (resolvedNode.type.name === "blockquote") {
                storage.blockType = {
                  type: "blockquote",
                  attrs: {},
                };
              } else {
                storage.blockType = null;
              }

              // Capture block-level attributes (textAlign, lineHeight)
              var blockAttrs = {};
              if (resolvedNode.attrs.textAlign) {
                blockAttrs.textAlign = resolvedNode.attrs.textAlign;
              }
              if (resolvedNode.attrs.lineHeight) {
                blockAttrs.lineHeight = resolvedNode.attrs.lineHeight;
              }
              storage.blockAttrs = blockAttrs;
            }

            storage.enabled = true;
            storage.once = once !== undefined ? once : self.options.once;

            editor.view.dispatch(
              editor.state.tr.setMeta("formatPainterAction", {
                type: "start",
                marks: storage.marks,
              })
            );
            return true;
          };
        },

      /**
       * Explicitly disable format painter.
       */
      unsetFormatPainter:
        function () {
          return function (ref) {
            var editor = ref.editor;
            var storage = editor.storage.formatPainter;
            storage.enabled = false;
            storage.marks = [];
            storage.blockType = null;
            storage.blockAttrs = {};
            editor.view.dispatch(
              editor.state.tr.setMeta("formatPainterAction", { type: "end" })
            );
            return true;
          };
        },
    };
  },

  addProseMirrorPlugins: function () {
    var self = this;
    return [
      new Plugin({
        key: new PluginKey("formatPainter"),
        state: {
          init: function () {
            return { enabled: false, marks: [], blockType: null, blockAttrs: {} };
          },
          apply: function (tr, pluginState) {
            var meta = tr.getMeta("formatPainterAction");
            if (meta) {
              return {
                enabled: meta.type === "start",
                marks: meta.type === "start" ? meta.marks : [],
                blockType: meta.type === "start" ? meta.blockType : null,
                blockAttrs: meta.type === "start" ? meta.blockAttrs : {},
              };
            }
            return pluginState;
          },
        },
        props: {
          handleDOMEvents: {
            mousedown: function (view, event) {
              var pluginState = self.editor.storage.formatPainter;
              if (!pluginState || !pluginState.enabled) return false;

              // Store the mouse target for the click handler
              view._formatPainterTarget = event.target;
              return false;
            },
            mouseup: function (view, event) {
              var pluginState = self.editor.storage.formatPainter;
              if (!pluginState || !pluginState.enabled) return false;

              var state = view.state;
              var sel = state.selection;

              // Check if there's a selection
              if (sel.empty) return false;

              var marks = pluginState.marks;
              var blockType = pluginState.blockType;
              var blockAttrs = pluginState.blockAttrs;

              var tr = state.tr;

              // Apply marks
              tr = tr.removeMark(sel.from, sel.to);
              for (var i = 0; i < marks.length; i++) {
                tr = tr.addMark(sel.from, sel.to, marks[i]);
              }

              // Apply block-level attributes and type
              if (blockType) {
                state.doc.nodesBetween(sel.from, sel.to, function (node, pos) {
                  if (node.type.isBlock && node.type.name !== "doc") {
                    var newAttrs = Object.assign({}, node.attrs, blockAttrs);
                    tr = tr.setNodeMarkup(pos, undefined, newAttrs);
                  }
                });
              }

              if (tr.docChanged) {
                view.dispatch(tr);
              }

              // If single-use (once), disable the painter
              if (pluginState.once) {
                pluginState.enabled = false;
                pluginState.marks = [];
                pluginState.blockType = null;
                pluginState.blockAttrs = {};
                view.dispatch(
                  view.state.tr.setMeta("formatPainterAction", { type: "end" })
                );
              }

              return true;
            },
          },
        },
      }),
    ];
  },
});

export default FormatPainter;
