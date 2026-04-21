import re

# 读取原始文件
with open('wecom.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# 修复每一行：去除行号格式
fixed_lines = []
for line in lines:
    # 去除行首的空格和行号格式（如 "     1|"）
    fixed_line = re.sub(r'^\s+\d+\|', '', line)
    fixed_lines.append(fixed_line)

# 写入修复后的文件
with open('wecom.py', 'w', encoding='utf-8') as f:
    f.writelines(fixed_lines)

print("文件修复完成")
