```markdown
# funbuild

funbuild 是一个构建工具，旨在简化 Python 项目的构建、发布和管理流程。

## 特性

- 支持多种构建类型，包括 PyPI、Poetry 和 UV。
- 自动化版本管理和发布流程。
- 支持 Git 操作，如拉取、推送和清理历史记录。
- 通过命令行界面（CLI）进行操作。
- 支持gcop自动生成提交信息

## 安装
确保您已安装 Python 3.8 及以上版本。您可以通过以下命令安装 funbuild：

```bash
pip install .
```


## 使用
在项目根目录下，您可以使用以下命令来管理您的构建流程：

- **升级版本**：
  ```bash
  funbuild upgrade
  ```

- **拉取代码**：
  ```bash
  funbuild pull
  ```
  

- **推送代码**：
  ```bash
  funbuild push --message "您的提交信息"
  ```

- **安装包**：
  ```bash
  funbuild install
  ```

- **构建发布**：
  ```bash
  funbuild build --message "您的提交信息"
  ```

- **清理历史**：
  ```bash
  funbuild clean_history
  ```

- **清理缓存**：
  ```bash
  funbuild clean
  ```

- **创建标签**：
  ```bash
  funbuild tags
  ```

## 配置

Funbuild 使用 `pyproject.toml` 文件进行配置。您可以在该文件中设置项目名称、版本、依赖项等信息。

## 许可证

本项目采用 MIT 许可证，详情请参阅 [LICENSE](LICENSE) 文件。

## 贡献

欢迎任何形式的贡献！请提交问题或拉取请求。

## 联系

如有任何问题，请联系作者：

- **姓名**: niuliangtao
- **邮箱**: farfarfun@qq.com
```

