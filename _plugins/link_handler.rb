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
