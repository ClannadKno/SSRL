import { Extension } from '@tiptap/core'

/**
 * LineHeight extension using a limited set of predefined values.
 * Values are stored as line-height CSS style on paragraph/heading nodes.
 */
export default Extension.create({
  name: 'lineHeight',

  addOptions() {
    return {
      types: ['paragraph', 'heading'],
      defaultLineHeight: 1.75,
    }
  },

  addGlobalAttributes() {
    return [
      {
        types: this.options.types,
        attributes: {
          lineHeight: {
            default: this.options.defaultLineHeight,
            parseHTML: (element) => {
              const lh = element.style.lineHeight
              if (!lh) return this.options.defaultLineHeight
              const num = Number(lh)
              return Number.isFinite(num) ? num : this.options.defaultLineHeight
            },
            renderHTML: (attributes) => {
              if (!attributes.lineHeight || attributes.lineHeight === this.options.defaultLineHeight) {
                return {}
              }
              return { style: `line-height: ${attributes.lineHeight}` }
            },
          },
        },
      },
    ]
  },

  addCommands() {
    return {
      setLineHeight:
        (lineHeight) =>
        ({ tr, state, dispatch }) => {
          const { doc, selection } = state
          let updated = false
          doc.nodesBetween(selection.from, selection.to, (node, pos) => {
            if (!this.options.types.includes(node.type.name)) return
            if (node.attrs.lineHeight === lineHeight) {
              updated = true
              return
            }
            tr = tr.setNodeMarkup(pos, undefined, {
              ...node.attrs,
              lineHeight,
            })
            updated = true
          })
          if (dispatch && tr.docChanged) {
            dispatch(tr)
          }
          return updated
        },
      unsetLineHeight:
        () =>
        ({ tr, state, dispatch }) => {
          const { doc, selection } = state
          const defaultVal = this.options.defaultLineHeight
          let updated = false
          doc.nodesBetween(selection.from, selection.to, (node, pos) => {
            if (!this.options.types.includes(node.type.name)) return
            if (!node.attrs.lineHeight || node.attrs.lineHeight === defaultVal) {
              updated = true
              return
            }
            tr = tr.setNodeMarkup(pos, undefined, {
              ...node.attrs,
              lineHeight: defaultVal,
            })
            updated = true
          })
          if (dispatch && tr.docChanged) {
            dispatch(tr)
          }
          return updated
        },
    }
  },
})
