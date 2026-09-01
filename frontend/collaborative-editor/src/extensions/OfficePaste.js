import { Extension } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

function isOfficeHtml(html) {
  if (!html) return false;
  var n = html.toLowerCase();
  return (
    n.indexOf("microsoft-com") !== -1 ||
    n.indexOf("urn:schemas-microsoft-com:office") !== -1 ||
    n.indexOf("mso-") !== -1 ||
    n.indexOf("w:worddocument") !== -1 ||
    n.indexOf("wps office") !== -1 ||
    n.indexOf("kingsoft") !== -1 ||
    n.indexOf("xmlns:wps") !== -1
  );
}

function hasImages(html) {
  if (!html) return false;
  return /<(img\b|v:imagedata\b|v:shape\b)/i.test(html);
}

function hasImagesInFiles(files) {
  for (var i = 0; i < files.length; i++) {
    if (files[i].type && files[i].type.indexOf("image/") === 0) return true;
  }
  return false;
}

function hasDangerousContent(html) {
  if (!html) return false;
  return (
    /<script\b/i.test(html) ||
    /\bon\w+\s*=/i.test(html) ||
    /<iframe\b/i.test(html) ||
    /javascript\s*:/i.test(html) ||
    /data:\s*text\/html/i.test(html)
  );
}

// Supported tags we want to preserve
var SUPPORTED_TAGS = {
  p: true, div: true, br: true,
  h1: true, h2: true, h3: true, h4: true, h5: true, h6: true,
  b: true, strong: true,
  i: true, em: true,
  u: true, ins: true,
  s: true, strike: true, del: true,
  ol: true, ul: true, li: true,
  blockquote: true,
  a: true,
  span: true,  // keep span but strip style/class if unwanted
  sub: true, sup: true,
  code: true, pre: true,
  mark: true,
  br: true,
};

/**
 * Thoroughly clean paste HTML:
 * - Remove all scripts, iframes, event handlers
 * - Remove office/tracking comments
 * - Remove class and id attributes
 * - Remove mso-* styles
 * - Remove font-family and font-size from inline styles
 * - Remove images (replace with text placeholder)
 * - Strip empty tags
 * - Only keep supported tags
 * - Clean link href values (no javascript:)
 * - Normalize list structures
 */
function cleanPasteHtml(html) {
  if (!html || html.trim().length === 0) return "";

  // Strip XML prolog / DOCTYPE for Office HTML
  html = html.replace(/<\?xml[^>]*\?>/gi, "");
  html = html.replace(/<!DOCTYPE[^>]*>/gi, "");
  html = html.replace(/<!--\[.*?\]-->/g, "");
  html = html.replace(/<!\s*\[if\s*[^\]]*\]>[\s\S]*?<!\s*\[endif\s*\]>/gi, "");
  html = html.replace(/<!--[\s\S]*?-->/g, "");

  // Remove <script> and <iframe> entirely
  html = html.replace(/<script\b[^<]*(?:<\/script>)[\s\S]*?<\/script>/gi, "");
  html = html.replace(/<script\b[^>]*\/?>/gi, "");
  html = html.replace(/<iframe\b[^>]*>[\s\S]*?<\/iframe>/gi, "");
  html = html.replace(/<iframe\b[^>]*\/?>/gi, "");

  // Remove event handlers (onclick, onload, etc.)
  html = html.replace(/\s+on\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, "");

  // Remove mso- prefixed tags and conditional comments
  html = html.replace(/<\/?o:\w+[^>]*>/gi, "");
  html = html.replace(/<\/?w:\w+[^>]*>/gi, "");
  html = html.replace(/<\/?v:\w+[^>]*>/gi, "");

  // Parse and walk the DOM to clean
  var parser = new DOMParser();
  var doc = parser.parseFromString(html, "text/html");

  // Process all nodes recursively
  function cleanNode(node) {
    if (node.nodeType === 3) return; // text node
    if (node.nodeType !== 1) {
      // Remove comments and other non-element nodes
      node.parentNode.removeChild(node);
      return;
    }

    var tag = node.tagName.toLowerCase();

    // Remove images and file objects
    if (tag === "img" || tag === "v:imagedata" || tag === "v:shape" || tag === "v:img") {
      var placeholder = doc.createTextNode(" [Image] ");
      node.parentNode.replaceChild(placeholder, node);
      return;
    }

    // Remove unsupported tags entirely
    if (!SUPPORTED_TAGS[tag] && tag !== "html" && tag !== "head" && tag !== "body" && tag !== "meta" && tag !== "style" && tag !== "title") {
      // Unwrap the content (keep children)
      while (node.firstChild) {
        node.parentNode.insertBefore(node.firstChild, node);
      }
      node.parentNode.removeChild(node);
      return;
    }

    // Strip class and id attributes
    if (tag !== "html" && tag !== "body") {
      node.removeAttribute("class");
      node.removeAttribute("id");
    }

    // Clean style attribute
    var style = node.getAttribute("style");
    if (style) {
      var cleaned = cleanInlineStyle(style);
      if (cleaned) {
        node.setAttribute("style", cleaned);
      } else {
        node.removeAttribute("style");
      }
    }

    // Clean link href
    if (tag === "a") {
      var href = node.getAttribute("href");
      if (href) {
        href = href.trim();
        if (/^\s*javascript\s*:/i.test(href)) {
          node.removeAttribute("href");
        } else if (/^\s*data\s*:/i.test(href)) {
          node.removeAttribute("href");
        } else if (/^\s*vbscript\s*:/i.test(href)) {
          node.removeAttribute("href");
        }
      }
      // Strip target if dangerous
      node.removeAttribute("rel");
    }

    // Remove data-* attributes
    var attrs = node.attributes;
    var toRemove = [];
    for (var i = 0; i < attrs.length; i++) {
      if (attrs[i].name.indexOf("data-") === 0) {
        toRemove.push(attrs[i].name);
      }
    }
    for (var r = 0; r < toRemove.length; r++) {
      node.removeAttribute(toRemove[r]);
    }

    // Recursively clean children
    var children = Array.from(node.childNodes);
    for (var c = 0; c < children.length; c++) {
      cleanNode(children[c]);
    }
  }

  // Clean the body content
  var body = doc.body;
  var children = Array.from(body.childNodes);
  for (var i = 0; i < children.length; i++) {
    cleanNode(children[i]);
  }

  // Remove empty nodes (except those that are self-closing like br)
  function removeEmptyNodes(parent) {
    var kids = Array.from(parent.childNodes);
    for (var k = 0; k < kids.length; k++) {
      var child = kids[k];
      if (child.nodeType !== 1) continue;
      var ct = child.tagName.toLowerCase();
      if (ct === "br" || ct === "img") continue;
      if (ct === "a" && child.getAttribute("href")) continue;
      removeEmptyNodes(child);
      if (!child.innerHTML || child.innerHTML.trim().length === 0) {
        if (child.parentNode) child.parentNode.removeChild(child);
      }
    }
  }
  removeEmptyNodes(body);

  // Extract body inner HTML
  var result = body.innerHTML;

  // Normalize excessive blank lines
  result = result.replace(/<p>\s*<\/p>/gi, "<p><br></p>");

  return result;
}

/**
 * Clean inline style attribute: remove font-*, mso-*, unwanted properties.
 * Keep: color, background-color, text-align, text-decoration.
 */
function cleanInlineStyle(style) {
  if (!style) return "";
  var parts = style.split(";");
  var keep = [];
  var allowedProperties = [
    "color", "background-color", "background",
    "text-align", "text-decoration",
    "font-weight", "font-style",
    "text-indent",
  ];
  for (var i = 0; i < parts.length; i++) {
    var part = parts[i].trim();
    if (!part) continue;
    // Skip mso-* properties
    if (/^\s*mso-/i.test(part)) continue;
    // Skip font-family and font-size
    if (/^\s*font-family\s*:/i.test(part)) continue;
    if (/^\s*font-size\s*:/i.test(part)) continue;
    // Skip proprietary properties
    if (/^\s*-ms-/i.test(part)) continue;
    if (/^\s*-webkit-/i.test(part)) continue;
    if (/^\s*-moz-/i.test(part)) continue;
    // Skip layout properties
    if (/^\s*(width|height|margin|padding|border|display|position|top|right|bottom|left|float|clear|overflow)\s*:/i.test(part)) continue;

    // Check if property name is in allowed list
    var propName = part.split(":")[0].trim().toLowerCase();
    var allowed = false;
    for (var a = 0; a < allowedProperties.length; a++) {
      if (propName === allowedProperties[a]) {
        allowed = true;
        break;
      }
    }
    if (allowed) {
      keep.push(part);
    }
  }
  return keep.join(";");
}

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

var OfficePaste = Extension.create({
  name: "officePaste",

  priority: 100, // high priority

  addOptions: function () {
    return {
      imagePlaceholder: " [Image] ",
      showImageWarning: true,
    };
  },

  addProseMirrorPlugins: function () {
    var self = this;

    return [
      new Plugin({
        key: new PluginKey("officePaste"),
        props: {
          /**
           * Intercept pasted HTML and clean it before ProseMirror processes it.
           */
          transformPastedHTML: function (html) {
            if (!html) return html;

            var cleaned = html;

            // Early return if empty
            if (cleaned.length === 0) return cleaned;

            // Detect Office/WPS content
            var isOffice = isOfficeHtml(cleaned);

            // Clean dangerous content regardless of source
            if (hasDangerousContent(cleaned)) {
              cleaned = cleanPasteHtml(cleaned);
            } else if (isOffice) {
              cleaned = cleanPasteHtml(cleaned);
            } else {
              // For regular web paste, still clean but less aggressively
              // Remove scripts, iframes, event handlers, images
              cleaned = cleaned.replace(/<script\b[^<]*(?:<\/script>)[\s\S]*?<\/script>/gi, "");
              cleaned = cleaned.replace(/<script\b[^>]*\/?>/gi, "");
              cleaned = cleaned.replace(/<iframe\b[^>]*>[\s\S]*?<\/iframe>/gi, "");
              cleaned = cleaned.replace(/<iframe\b[^>]*\/?>/gi, "");
              cleaned = cleaned.replace(/\s+on\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, "");
              cleaned = cleaned.replace(/<img\b[^>]*>/gi, " [Image] ");

              // Strip class and id attributes
              cleaned = cleaned.replace(/\s(class|id)=("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/gi, "");

              // Clean href javascript:
              cleaned = cleaned.replace(/href\s*=\s*"(?:javascript|data|vbscript):[^"]*"/gi, 'href=""');
              cleaned = cleaned.replace(/href\s*=\s*'(?:javascript|data|vbscript):[^']*'/gi, "href=''");
            }

            return cleaned;
          },

          /**
           * Handle paste event for additional checks (images, files).
           */
          handlePaste: function (view, event) {
            var clipboardData = event.clipboardData;
            if (!clipboardData) return false;

            var html = clipboardData.getData("text/html") || "";
            var files = Array.from(clipboardData.files || []);
            var text = clipboardData.getData("text/plain") || "";

            // Check for images in files
            var hasImageFiles = hasImagesInFiles(files);

            // Check for images in HTML
            var hasImageHtml = hasImages(html);

            // If we have images, prevent default paste and show a warning
            if (hasImageFiles || hasImageHtml) {
              if (self.options.showImageWarning) {
                // Dispatch a custom event for the app to show a toast
                var warnEvent = new CustomEvent("paste-image-blocked", {
                  detail: {
                    message: "Images cannot be pasted directly. Please use the text content only.",
                  },
                });
                window.dispatchEvent(warnEvent);
              }
              // Don't prevent the paste entirely; the transformPastedHTML will replace
              // images with placeholders. Let it continue.
            }

            // If there's no HTML but there are files, prevent paste
            if (files.length > 0 && !html && !text) {
              event.preventDefault();
              var fileEvent = new CustomEvent("paste-image-blocked", {
                detail: {
                  message: "Files cannot be pasted directly. Please use the text content only.",
                },
              });
              window.dispatchEvent(fileEvent);
              return true;
            }

            return false; // Let default paste handling continue
          },
        },
      }),
    ];
  },
});

export { cleanPasteHtml, isOfficeHtml, cleanInlineStyle };
export default OfficePaste;
