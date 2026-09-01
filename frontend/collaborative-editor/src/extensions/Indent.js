import { Extension } from '@tiptap/core'

/**
 * Indent / outdent extension for paragraphs, headings, and list items.
 * Stores indent as a numeric level. Max level = 8.
 */
export default Extension.create({
  name: 'indent',

  addOptions() {
    return {
      types: ['paragraph', 'heading', 'listItem', 'taskItem'],
      minLevel: 0,
      maxLevel: 8,
    }
  },

  addGlobalAttributes() {
    return [
      {
        types: this.options.types,
        attributes: {
          indent: {
            default: null,
            parseHTML: (element) => {
              const textIndent = element.style.textIndent
              const marginLeft = element.style.marginLeft
              if (marginLeft) {
                const match = marginLeft.match(/^(\d+(?:\.\d+)?)em$/)
                if (match) {
                  return Math.round(Number(match[1]))
                }
              }
              if (textIndent) {
                const match = textIndent.match(/^(\d+(?:\.\d+)?)em$/)
                if (match) {
                  return Math.round(Number(match[1]))
                }
              }
              return null
            },
            renderHTML: (attributes) => {
              if (!attributes.indent || attributes.indent <= 0) return {}
              const val = attributes.indent * 2
              return { style: `margin-left: ${val}em` }
            },
          },
        },
      },
    ]
  },

  addCommands() {
    return {
      setIndent:
        () =>
        ({ tr, state, dispatch }) => {
          const { doc, selection } = state
          let updated = false
          doc.nodesBetween(selection.from, selection.to, (node, pos) => {
            if (!this.options.types.includes(node.type.name)) return
            if (node.attrs.indent >= this.options.maxLevel) {
              updated = true
              return
            }
            const nextLevel = (node.attrs.indent || 0) + 1
            if (nextLevel > this.options.maxLevel) return
            tr = tr.setNodeMarkup(pos, undefined, {
              ...node.attrs,
              indent: nextLevel,
            })
            updated = true
          })
          if (dispatch && tr.docChanged) {
            dispatch(tr)
          }
          return updated
        },
      setOutdent:
        () =>
        ({ tr, state, dispatch }) => {
          const { doc, selection } = state
          let updated = false
          doc.nodesBetween(selection.from, selection.to, (node, pos) => {
            if (!this.options.types.includes(node.type.name)) return
            if (!node.attrs.indent || node.attrs.indent <= 0) {
              updated = true
              return
            }
            const nextLevel = node.attrs.indent - 1
            const newAttrs = nextLevel <= 0
              ? { ...node.attrs, indent: null }
              : { ...node.attrs, indent: nextLevel }
            tr = tr.setNodeMarkup(pos, undefined, newAttrs)
            updated = true
          })
          if (dispatch && tr.docChanged) {
            dispatch(tr)
          }
          return updated
        },
    }
  },

  addKeyboardShortcuts() {
    return {
      Tab: () => {
        if (
          this.editor.isActive('bulletList') ||
          this.editor.isActive('orderedList')
        ) {
          return this.editor.commands.sinkListItem('listItem')
        }
        if (this.editor.isActive('taskList')) {
          return this.editor.commands.sinkListItem('taskItem')
        }
        return this.editor.commands.setIndent()
      },
      'Shift-Tab': () => {
        if (
          this.editor.isActive('bulletList') ||
          this.editor.isActive('orderedList')
        ) {
          return this.editor.commands.liftListItem('listItem')
        }
        if (this.editor.isActive('taskList')) {
          return this.editor.commands.liftListItem('taskItem')
        }
        return this.editor.commands.setOutdent()
      },
    }
  },
})
