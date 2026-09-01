import { Extension } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";

function buildRegex(searchTerm, caseSensitive) {
  var escaped = searchTerm.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  var flags = caseSensitive ? "gu" : "gui";
  return new RegExp(escaped, flags);
}

function processSearches(doc, regex, resultIndex) {
  var decorations = [];
  var results = [];
  var textNodes = [];
  var blockIndex = 0;
  if (!regex) return { decorationsToReturn: DecorationSet.empty, results: [] };
  doc.descendants(function (node, pos) {
    if (node.isText) {
      if (textNodes[blockIndex]) {
        textNodes[blockIndex] = { text: textNodes[blockIndex].text + node.text, pos: textNodes[blockIndex].pos };
      } else {
        textNodes[blockIndex] = { text: node.text, pos: pos };
      }
    } else {
      blockIndex += 1;
    }
  });
  textNodes = textNodes.filter(Boolean);
  for (var t = 0; t < textNodes.length; t++) {
    var entry = textNodes[t];
    var matches = Array.from(entry.text.matchAll(regex));
    for (var m = 0; m < matches.length; m++) {
      var match = matches[m];
      if (match.index === undefined || match[0].length === 0) continue;
      results.push({ from: entry.pos + match.index, to: entry.pos + match.index + match[0].length });
    }
  }
  for (var i = 0; i < results.length; i++) {
    var r = results[i];
    var cls = "umo-search-result";
    if (i === resultIndex) cls += " umo-search-result-current";
    decorations.push(Decoration.inline(r.from, r.to, { class: cls }));
  }
  return { decorationsToReturn: DecorationSet.create(doc, decorations), results: results };
}

function replaceCurrent(replaceTerm, results, state, dispatch) {
  if (!results || results.length === 0) return false;
  var r = results[0];
  if (dispatch) dispatch(state.tr.insertText(replaceTerm, r.from, r.to));
  return true;
}

function replaceAll(replaceTerm, results, tr, dispatch) {
  if (!results || results.length === 0) return false;
  var offset = 0;
  for (var i = 0; i < results.length; i++) {
    var r = results[i];
    var adjustedFrom = r.from - offset;
    var adjustedTo = r.to - offset;
    tr.insertText(replaceTerm, adjustedFrom, adjustedTo);
    offset += (r.to - r.from) - replaceTerm.length;
  }
  if (dispatch) dispatch(tr);
  return true;
}

function createSearchPlugin(editor, options) {
  var searchResultClass = options.searchResultClass || "umo-search-result";
  return new Plugin({
    key: new PluginKey("searchReplace"),
    state: {
      init: function () { return DecorationSet.empty; },
      apply: function (tr, oldState, oldEditorState, newEditorState) {
        var storage = editor.storage.searchReplace;
        var searchTerm = storage.searchTerm;
        var caseSensitive = storage.caseSensitive;
        var resultIndex = storage.resultIndex;
        var docChanged = tr.docChanged || tr.getMeta("searchReplaceUpdate");
        var termChanged = searchTerm !== storage._lastSearchTerm;
        var caseChanged = caseSensitive !== storage._lastCaseSensitive;
        var indexChanged = resultIndex !== storage._lastResultIndex;
        if (!docChanged && !termChanged && !caseChanged && !indexChanged) return oldState;
        storage._lastSearchTerm = searchTerm;
        storage._lastCaseSensitive = caseSensitive;
        storage._lastResultIndex = resultIndex;
        if (!searchTerm || searchTerm.length === 0) {
          storage.results = [];
          return DecorationSet.empty;
        }
        var regex = buildRegex(searchTerm, caseSensitive);
        var processed = processSearches(newEditorState.doc, regex, resultIndex);
        storage.results = processed.results;
        return processed.decorationsToReturn;
      },
    },
    props: {
      decorations: function (state) { return this.getState(state); },
    },
  });
}

var SearchReplace = Extension.create({
  name: "searchReplace",
  addOptions: function () { return { searchResultClass: "umo-search-result" }; },
  addStorage: function () {
    return { searchTerm: "", replaceTerm: "", results: [], resultIndex: 0, caseSensitive: false, _lastSearchTerm: null, _lastCaseSensitive: null, _lastResultIndex: null };
  },
  addCommands: function () {
    return {
      setSearchTerm: function (searchTerm) { return function (ref) { ref.editor.storage.searchReplace.searchTerm = searchTerm; ref.editor.view.dispatch(ref.editor.state.tr.setMeta("searchReplaceUpdate", true)); return false; }; },
      setReplaceTerm: function (replaceTerm) { return function (ref) { ref.editor.storage.searchReplace.replaceTerm = replaceTerm; return false; }; },
      setCaseSensitive: function (caseSensitive) { return function (ref) { ref.editor.storage.searchReplace.caseSensitive = caseSensitive; ref.editor.view.dispatch(ref.editor.state.tr.setMeta("searchReplaceUpdate", true)); return false; }; },
      resetIndex: function () { return function (ref) { ref.editor.storage.searchReplace.resultIndex = 0; return false; }; },
      nextSearchResult: function () { return function (ref) { var editor = ref.editor; var storage = editor.storage.searchReplace; if (storage.results.length === 0) return false; storage.resultIndex = (storage.resultIndex + 1) % storage.results.length; var pos = storage.results[storage.resultIndex]; if (pos) { editor.commands.setTextSelection(pos); editor.view.dispatch(editor.state.tr.setMeta("searchReplaceUpdate", true)); } return false; }; },
      previousSearchResult: function () { return function (ref) { var editor = ref.editor; var storage = editor.storage.searchReplace; if (storage.results.length === 0) return false; storage.resultIndex = (storage.resultIndex - 1 + storage.results.length) % storage.results.length; var pos = storage.results[storage.resultIndex]; if (pos) { editor.commands.setTextSelection(pos); editor.view.dispatch(editor.state.tr.setMeta("searchReplaceUpdate", true)); } return false; }; },
      replaceCurrent: function () { return function (ref) { var editor = ref.editor, state = ref.state, dispatch = ref.dispatch; var storage = editor.storage.searchReplace; return replaceCurrent(storage.replaceTerm, storage.results, state, dispatch); }; },
      replaceAll: function () { return function (ref) { var editor = ref.editor, tr = ref.tr, dispatch = ref.dispatch; var storage = editor.storage.searchReplace; return replaceAll(storage.replaceTerm, storage.results.slice(), tr, dispatch); }; },
    };
  },
  addProseMirrorPlugins: function () { return [createSearchPlugin(this.editor, this.options)]; },
});

export { SearchReplace, buildRegex, processSearches, replaceCurrent, replaceAll };
export default SearchReplace;
