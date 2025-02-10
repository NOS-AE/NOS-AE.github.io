require 'kramdown'

module Kramdown
  module Converter
    class Html
      # 保存原始的 convert_a 方法
      alias_method :old_convert_a, :convert_a
      
      # 重写 convert_a 方法
      def convert_a(el, indent)
        # 获取原始的 HTML
        html = old_convert_a(el, indent)
        
        # 如果已经有 target 属性，直接返回
        return html if html.include?('target=')
        
        # 在第一个 > 之前插入 target="_blank"
        html.sub!('>', ' target="_blank">')
        html
      end
    end
  end
end

module Jekyll
  class LinkHandler < Generator
    def generate(site)
      site.posts.docs.each do |post|
        post.content = process_content(post.content)
      end
    end

    private

    def process_content(content)
      lines = content.split("\n")
      
      lines.map do |line|
        # 跳过包含图片的行（包括Markdown格式和HTML格式）
        next line if line.match?(/!\[.*?\]\(.*?\)/) || line.match?(/<img[^>]+>/i)
        # 跳过已经是Markdown链接的行
        next line if line.match?(/\[.+?\]\(.+?\)/)
        # 跳过包含HTML链接的行
        next line if line.match?(/<a\s.*?>.*?<\/a>/)
        
        # 处理独立的URL
        line.gsub(/(?<![\[!])\b(https?:\/\/[^\s<>\[\]()'"]+)(?!\))/) do |url|
          "<a href=\"#{url}\" target=\"_blank\">#{url}</a>"
        end
      end.join("\n")
    end
  end
end
