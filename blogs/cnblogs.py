import xmlrpc.client
import re
import sys

class CnblogsClient:
    def __init__(self, config):
        self.url = config['url']
        self.username = config['username']
        self.token = config['token']
        self.client = xmlrpc.client.ServerProxy(self.url)
        self.blog_id = self._get_blog_id()

    def _get_blog_id(self):
        """获取博客ID"""
        blogs = self.client.blogger.getUsersBlogs("appkey", self.username, self.token)
        return blogs[0]['blogid']

    def find_post_by_title(self, title):
        """查找指定标题的文章"""
        try:
            # 获取最近的100篇文章
            recent_posts = self.client.metaWeblog.getRecentPosts(
                self.blog_id, self.username, self.token, 100
            )
            for post in recent_posts:
                if post.get('title') == title:
                    return post.get('postid')
        except Exception as e:
            print(f"查找文章时发生错误：{str(e)}")
        return None

    def edit_post(self, post_id, post):
        """编辑指定ID的文章"""
        try:
            result = self.client.metaWeblog.editPost(
                post_id, self.username, self.token, post, True
            )
            if result:
                print(f"成功更新文章 ID: {post_id}")
                return True
        except Exception as e:
            print(f"更新文章时发生错误：{str(e)}")
        return False

    def new_post(self, post):
        """发布新文章"""
        try:
            post_id = self.client.metaWeblog.newPost(
                self.blog_id, self.username, self.token, post, True
            )
            print(f"发布成功，文章 ID: {post_id}")
            return post_id
        except Exception as e:
            print(f"发布文章失败：{str(e)}")
            return None

def process_content(content):
    """处理文章内容，删除 alert 风格的引用标记"""
    # 匹配形如 > [!note] 或 > [!warning] 等 alert 标记
    pattern = r'>\s*\[\!.*?\]\s*\n>'
    # 替换为单个 >
    return re.sub(pattern, '>', content)

def parse_markdown_file(file_path):
    """解析Markdown文件"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"错误：文件 {file_path} 不存在")
        sys.exit(1)
    except Exception as e:
        print(f"读取文件时发生错误：{str(e)}")
        sys.exit(1)

    # 使用正则表达式匹配 front matter
    pattern = r'^---\n(.*?)\n---\n(.*)'
    match = re.match(pattern, content, re.DOTALL)
    
    if not match:
        print("错误：无法解析文件格式，请确保文件包含正确的 front matter")
        sys.exit(1)
    
    front_matter = match.group(1)
    post_content = match.group(2)

    # 处理文章内容，删除 alert 风格的引用标记
    post_content = process_content(post_content)

    # 解析 front matter
    title = None
    categories = []
    tags = None

    for line in front_matter.split('\n'):
        if line.startswith('title:'):
            title = line[6:].strip()
        elif line.startswith('categories:'):
            categories_str = line[11:].strip()
            if categories_str.startswith('[') and categories_str.endswith(']'):
                categories = [cat.strip() for cat in categories_str[1:-1].split(',')]
        elif line.startswith('tags:'):
            tags_str = line[5:].strip()
            if tags_str.startswith('[') and tags_str.endswith(']'):
                tags = tags_str.strip()[1:-1]

    if not title:
        print("错误：未找到文章标题")
        sys.exit(1)

    categories.append('[Markdown]')
    return {
        'title': title,
        'categories': categories,
        'tags': tags,
        'content': post_content
    }

def sync_to_cnblogs(file_path, config):
    """同步文章到博客园"""
    # 初始化客户端
    client = CnblogsClient(config)

    # 解析 Markdown 文件
    post_data = parse_markdown_file(file_path)

    # 构造文章内容
    post = {
        "title": post_data['title'],
        "description": post_data['content']
    }

    if post_data['categories']:
        post["categories"] = post_data['categories']
    if post_data['tags']:
        post["mt_keywords"] = 'kafka,mq,源码'  # post_data['tags']

    # 检查是否存在相同标题的文章
    existing_post_id = client.find_post_by_title(post_data['title'])
    if existing_post_id:
        print(f"发现相同标题的文章，正在更新...")
        if not client.edit_post(existing_post_id, post):
            print("更新文章失败，程序退出")
            sys.exit(1)
    else:
        client.new_post(post) 